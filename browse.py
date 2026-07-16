#!/usr/bin/env python3
"""
Music library tree, tag I/O, and edit logic — the UI-agnostic core behind the web
Browse view (server.py imports build_tree, read_tags/write_tags, search,
albums_by_genre, build_edit/apply_edits, reorder_album, and the artwork helpers).

Expected structure: root/Album Artist/Album/mp3s

Edit operations (build_edit op names):
  artist_rename / artist_genre          — across all of an artist's albums
  album_title / album_year /
  album_artist / album_genre / album_merge
  track_title / track_artist
All edits are built as pure PendingEdit objects and committed via apply_edits().
"""

import re
import shutil
from pathlib import Path

import settings as settings_mod
from fetch_art import (
    CONFIDENT_MATCH_SCORE,
    fetch_artwork,
    resize_artwork,
    search_art_sources,
)
from chars import (
    extract_year as _extract_year,
    normalize as _normalize,
    sanitize as _sanitize,
)

from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    APIC as _APIC,
    TPE1 as _TPE1, TPE2 as _TPE2, TIT2 as _TIT2, TALB as _TALB,
    TYER as _TYER, TDRC as _TDRC, TCON as _TCON, TRCK as _TRCK,
)


# ── Character normalization ───────────────────────────────────────────────────

_ALBUM_YEAR_RE = re.compile(r"^\d{4}\s*-\s*(.+)$")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# TPE2 is the canonical album artist frame. The TXXX variants are legacy —
# read them for migration but never write them (see standardize.ALBUM_ARTIST_KEYS).
_ALBUM_ARTIST_KEYS = (
    "TPE2",
    "TXXX:album artist",
    "TXXX:ALBUMARTIST",
    "TXXX:ALBUM ARTIST",
    "TXXX:AlbumArtist",
    "TXXX:Album Artist",
)


def _load_id3(path: Path) -> ID3:
    """Load raw ID3 frames without mutagen's v2.4 translation layer."""
    return ID3(path, translate=False)


def _album_artist_value(tags: ID3) -> str:
    for key in _ALBUM_ARTIST_KEYS:
        frame = tags.get(key)
        if frame and hasattr(frame, "text") and frame.text:
            return str(frame.text[0])
    return ""


def _set_album_artist(tags: ID3, value: str) -> None:
    for key in _ALBUM_ARTIST_KEYS:
        if key != "TPE2" and key in tags:
            del tags[key]
    # encoding=1 (UTF-16): the only non-latin1 encoding valid in ID3v2.3, and
    # what standardize/import write. (mutagen would downgrade 3→1 on save, but
    # write the v2.3-native value directly.)
    tags["TPE2"] = _TPE2(encoding=1, text=value)


# ── Node model ────────────────────────────────────────────────────────────────

ARTIST = "artist"
ALBUM  = "album"
TRACK  = "track"


class Node:
    __slots__ = ("kind", "label", "path", "parent", "children",
                 "expanded", "tags", "loaded")

    def __init__(self, kind: str, label: str, path: Path, parent: "Node | None" = None):
        self.kind     = kind
        self.label    = label
        self.path     = path
        self.parent   = parent
        self.children: list["Node"] = []
        self.expanded = False
        self.tags: dict[str, str] = {}
        self.loaded   = False


# ── Tree construction ─────────────────────────────────────────────────────────

def _mp3s(path: Path) -> list[Path]:
    return sorted(path.glob("*.mp3"))


def _subdirs(path: Path) -> list[Path]:
    return sorted(d for d in path.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def _make_tracks(mp3s: list[Path], parent: Node) -> list[Node]:
    """Track nodes for *mp3s* under *parent* (also used by server._apply_album_art)."""
    return [Node(TRACK, mp3.name, mp3, parent=parent) for mp3 in mp3s]


# root(str) -> (signature, snapshot). The tree's shape (folders + track
# filenames) can only change by changing some directory's contents, which bumps
# that directory's mtime — so a dir-level mtime signature is a complete
# invalidation key (same pattern as album_stream._CACHE). File *content* edits
# don't need to invalidate: the tree stores names, not tags.
_TREE_CACHE: dict[str, tuple[tuple, list]] = {}


def _tree_signature(root: Path) -> tuple:
    """(name, mtime_ns) for root and its first two directory levels — the
    levels whose listings build_tree reads. Empty tuple = unstat-able (no
    caching)."""
    sig: list[tuple[str, int]] = []
    try:
        sig.append(("", root.stat().st_mtime_ns))
        for d1 in root.iterdir():
            if d1.is_dir() and not d1.name.startswith("."):
                sig.append((d1.name, d1.stat().st_mtime_ns))
                for d2 in d1.iterdir():
                    if d2.is_dir() and not d2.name.startswith("."):
                        sig.append((f"{d1.name}/{d2.name}", d2.stat().st_mtime_ns))
    except OSError:
        return ()
    return tuple(sorted(sig))


def _scan_tree(root: Path) -> list:
    """Walk the filesystem once, returning the tree as plain data:
    [(artist_label, artist_path, [(album_label, album_path, [mp3 names])])]."""
    def albums_of(parent: Path) -> list:
        out = []
        for album_dir in _subdirs(parent):
            mp3s = _mp3s(album_dir)
            if mp3s:
                out.append((album_dir.name, str(album_dir),
                            [p.name for p in mp3s]))
        return out

    direct = _mp3s(root)
    if direct:
        return [(root.name, str(root),
                 [(root.name, str(root), [p.name for p in direct])])]

    child_dirs = _subdirs(root)
    if any(_mp3s(d) for d in child_dirs):
        albums = albums_of(root)
        return [(root.name, str(root), albums)] if albums else []

    artists = []
    for artist_dir in child_dirs:
        albums = albums_of(artist_dir)
        if albums:
            artists.append((artist_dir.name, str(artist_dir), albums))
    return artists


def _nodes_from(snapshot: list) -> list[Node]:
    """Fresh Node objects from a snapshot — callers mutate nodes (labels, tags,
    expanded), so cached snapshots must never share Node instances."""
    artists: list[Node] = []
    for a_label, a_path, albums in snapshot:
        artist = Node(ARTIST, a_label, Path(a_path))
        for al_label, al_path, names in albums:
            album_dir = Path(al_path)
            album = Node(ALBUM, al_label, album_dir, parent=artist)
            album.children = [Node(TRACK, name, album_dir / name, parent=album)
                              for name in names]
            artist.children.append(album)
        artists.append(artist)
    return artists


def build_tree(root: Path) -> list[Node]:
    """
    Build a Node tree from *root*, auto-detecting which level it represents.

    Library root  root/Album Artist/Album/mp3   → 3-level tree
    Album artist dir root/Album/mp3             → 2-level tree
    Album dir     root/mp3                      → 1-level

    The filesystem walk is cached per root behind a dir-mtime signature, so
    repeated calls (every Browse/search/genre request) don't re-walk an
    unchanged library.
    """
    key = str(root)
    sig = _tree_signature(root)
    cached = _TREE_CACHE.get(key)
    if not (sig and cached and cached[0] == sig):
        cached = (sig, _scan_tree(root))
        if sig:
            _TREE_CACHE[key] = cached
    return _nodes_from(cached[1])


# ── Tag I/O ───────────────────────────────────────────────────────────────────

def _fmt_dur(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


# path(str) -> ((mtime_ns, size), tags). Tag reads dominate the album-grid and
# genre endpoints (first track of every album per request); a stat-signature
# cache turns repeat visits into one stat() per file.
_TAG_CACHE: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def _read_tags(path: Path) -> dict[str, str]:
    try:
        st = path.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    cached = _TAG_CACHE.get(str(path))
    if cached and cached[0] == sig:
        return dict(cached[1])          # copy: callers may mutate
    tags = _read_tags_uncached(path)
    if tags:
        _TAG_CACHE[str(path)] = (sig, dict(tags))
    return tags


def _read_tags_uncached(path: Path) -> dict[str, str]:
    try:
        audio = MP3(path, ID3=lambda *a, **kw: ID3(*a, translate=False, **kw))
        t = _load_id3(path)
        def g(k: str) -> str:
            f = t.get(k)
            return str(f.text[0]) if f and hasattr(f, "text") else ""
        result = {
            "title":  g("TIT2"),
            "artist": g("TPE1"),
            "albumartist": _album_artist_value(t),
            "album":  g("TALB"),
            "year":   g("TYER") or g("TDRC"),
            "genre":  g("TCON"),
            "track":  g("TRCK"),
        }
        if audio.info:
            result["bitrate"] = str(int(audio.info.bitrate / 1000))
            result["length"] = _fmt_dur(audio.info.length)
            result["length_sec"] = int(audio.info.length or 0)
        return result
    except Exception:
        return {}


def _track_label(tags: dict[str, str], fallback: str) -> str:
    title = tags.get("title", "")
    if not title:
        return fallback
    raw = tags.get("track", "").split("/")[0].strip()
    num = raw.zfill(2) if raw.isdigit() else raw
    return f"{num}. {title}" if num else title


def load_album_tags(album: Node) -> None:
    if album.loaded:
        return
    for track in album.children:
        track.tags  = _read_tags(track.path)
        track.label = _track_label(track.tags, track.path.name)
    album.loaded = True


def _write_tags(path: Path, updates: dict[str, str]) -> None:
    _CLS = {
        "TPE1": _TPE1, "TIT2": _TIT2, "TALB": _TALB,
        "TYER": _TYER, "TDRC": _TDRC, "TCON": _TCON, "TRCK": _TRCK,
    }
    try:
        tags = _load_id3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for frame_id, value in updates.items():
        if frame_id == "ALBUMARTIST":
            _set_album_artist(tags, value)
            continue
        cls = _CLS.get(frame_id)
        if cls:
            tags[frame_id] = cls(encoding=1, text=value)
    tags.save(path, v2_version=3, v1=0)
    # A mutagen re-save can leave size AND (at coarse fs granularity) mtime
    # unchanged, so the stat signature alone may miss this write.
    _TAG_CACHE.pop(str(path), None)


# ── Public API (stable surface for non-curses consumers, e.g. server.py) ──────
# These names are part of the supported interface; the leading-underscore
# originals stay as the TUI's internal call sites. Keep behaviour identical.

def read_tags(path: Path) -> dict[str, str]:
    """Read ID3 tags from *path* as a plain dict (see _read_tags)."""
    return _read_tags(path)


def track_label(tags: dict[str, str], fallback: str) -> str:
    """Human label for a track given its tag dict (see _track_label)."""
    return _track_label(tags, fallback)


def write_tags(path: Path, updates: dict[str, str]) -> None:
    """Write tag *updates* (frame ids; ``ALBUMARTIST`` special) to *path*."""
    _write_tags(path, updates)


def album_search_terms(album: Node) -> tuple[str, str]:
    """Return (artist, album_title) for an art search from an ALBUM node."""
    return _album_search_terms(album)


def apply_art_to_album(album: Node, data: bytes, mime: str,
                       cover_art: str, cover_art_size: int) -> tuple[int, int]:
    """Write/embed art for *album*. Returns (updated, errors)."""
    return _apply_art_to_album(album, data, mime, cover_art, cover_art_size)


# ── Visible flat list ─────────────────────────────────────────────────────────

def visible(artists: list[Node]) -> list[Node]:
    out: list[Node] = []
    for artist in artists:
        out.append(artist)
        if artist.expanded:
            for album in artist.children:
                out.append(album)
                if album.expanded:
                    out.extend(album.children)
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _n_albums(node: Node) -> int:
    return len(node.children)


def _n_tracks(node: Node) -> int:
    if node.kind == ALBUM:
        return len(node.children)
    return sum(len(a.children) for a in node.children)


def _track_num(track: Node) -> int | None:
    raw = track.tags.get("track", "").split("/")[0].strip()
    return int(raw) if raw.isdigit() else None


def _track_width(album: Node) -> int:
    return 3 if len(album.children) >= 100 else 2


# ── Pending-edit model ────────────────────────────────────────────────────────

class PendingEdit:
    def __init__(self, desc: str):
        self.desc            = desc
        self.tag_writes:     list[tuple[Path, dict[str, str]]] = []
        self.file_renames:   list[tuple[Path, Path]]           = []
        self.dir_renames:    list[tuple[Path, Path]]           = []
        self.dir_removals:   list[Path]                        = []
        self.preview_labels: dict[int, str]                    = {}


# ── Edit builders ─────────────────────────────────────────────────────────────

def _new_track_filename(num: int, width: int, artist_s: str, title_s: str) -> str:
    return f"{str(num).zfill(width)}. {artist_s} - {title_s}.mp3"


def _build_artist_rename(artist: Node, raw: str) -> "PendingEdit | None":
    new_name = _sanitize(raw)
    if not new_name:
        return None
    new_tag = _normalize(raw)
    for album in artist.children:
        load_album_tags(album)

    edit = PendingEdit(f"Album artist rename: {artist.label!r} → {new_name!r}")
    edit.preview_labels[id(artist)] = new_name

    for album in artist.children:
        for track in album.children:
            if track.tags:
                edit.tag_writes.append((track.path, {"ALBUMARTIST": new_tag}))

    new_dir = artist.path.parent / new_name
    if new_dir != artist.path:
        edit.dir_renames.append((artist.path, new_dir))
    return edit


def _build_artist_genre(artist: Node, raw: str) -> "PendingEdit | None":
    new_genre = _normalize(raw)
    if not new_genre:
        return None
    for album in artist.children:
        load_album_tags(album)
    edit = PendingEdit(f"Artist genre → {new_genre!r}")
    for album in artist.children:
        for track in album.children:
            if track.tags:
                edit.tag_writes.append((track.path, {"TCON": new_genre}))
    return edit


def _build_album_title(album: Node, raw: str) -> "PendingEdit | None":
    new_title = _normalize(raw)
    if not new_title:
        return None
    load_album_tags(album)
    year = ""
    for tr in album.children:
        y = tr.tags.get("year", "")
        if y:
            year = _extract_year(y) or ""
            break
    folder = _sanitize(f"{year} - {new_title}") if year else _sanitize(new_title)
    new_dir = album.path.parent / folder

    if new_dir != album.path and new_dir.exists():
        existing_node = next(
            (a for a in (album.parent.children if album.parent else [])
             if a.path == new_dir and a is not album),
            None,
        )
        return _build_album_merge(album, new_title, new_dir, existing_node)

    edit = PendingEdit(f"Album title: {album.label!r} → {folder!r}")
    edit.preview_labels[id(album)] = folder
    for track in album.children:
        if track.tags:
            edit.tag_writes.append((track.path, {"TALB": new_title}))
    if new_dir != album.path:
        edit.dir_renames.append((album.path, new_dir))
    return edit


def _build_album_merge(
    album: Node, new_title: str, new_dir: Path, existing_node: "Node | None"
) -> PendingEdit:
    """Append album's tracks after those of the existing album at new_dir."""
    load_album_tags(album)

    if existing_node is not None:
        load_album_tags(existing_node)
        existing_paths = [tr.path for tr in existing_node.children]
    else:
        existing_paths = sorted(new_dir.glob("*.mp3"))

    n_existing = len(existing_paths)
    n_added    = len(album.children)
    new_total  = n_existing + n_added
    width      = 3 if new_total >= 100 else 2

    edit = PendingEdit(f"Album merge: {album.label!r} → {new_dir.name!r}")
    edit.preview_labels[id(album)] = f"{new_dir.name}  [→ merge]"

    # Update TRCK totals for existing album's tracks (positions unchanged)
    for i, path in enumerate(existing_paths, 1):
        edit.tag_writes.append((path, {"TRCK": f"{i}/{new_total}"}))

    # Append renamed album's tracks with new sequential numbers
    for i, track in enumerate(album.children, n_existing + 1):
        if not track.tags:
            continue
        artist_s  = _sanitize(track.tags.get("artist", ""))
        title_s   = _sanitize(track.tags.get("title", ""))
        new_fname = _new_track_filename(i, width, artist_s, title_s)
        new_path  = new_dir / new_fname
        edit.tag_writes.append((track.path, {"TALB": new_title, "TRCK": f"{i}/{new_total}"}))
        if new_path != track.path:
            edit.file_renames.append((track.path, new_path))

    # Move cover art if the target album has none
    try:
        has_cover = any(
            f.is_file() and f.suffix.lower() in _IMAGE_EXTENSIONS
            for f in new_dir.iterdir()
        )
    except OSError:
        has_cover = True
    if not has_cover:
        try:
            for f in sorted(album.path.iterdir()):
                if f.is_file() and f.suffix.lower() in _IMAGE_EXTENSIONS:
                    edit.file_renames.append((f, new_dir / f.name))
                    break
        except OSError:
            pass

    edit.dir_removals.append(album.path)
    return edit


def _build_album_year(album: Node, raw: str) -> "PendingEdit | None":
    year = _extract_year(raw)
    if not year:
        return None
    load_album_tags(album)
    album_title = ""
    for tr in album.children:
        album_title = tr.tags.get("album", "")
        if album_title:
            break
    folder  = _sanitize(f"{year} - {album_title}") if album_title else year
    new_dir = album.path.parent / folder

    edit = PendingEdit(f"Album year: {album.label!r} → {folder!r}")
    edit.preview_labels[id(album)] = folder
    for track in album.children:
        if not track.tags:
            continue
        updates: dict[str, str] = {"TYER": year}
        # update TDRC only if it was already present (we stored year from either frame)
        # safest: always set TYER; leave TDRC alone unless it appears in tag file
        edit.tag_writes.append((track.path, updates))
    if new_dir != album.path:
        edit.dir_renames.append((album.path, new_dir))
    return edit


def _build_album_genre(album: Node, raw: str) -> "PendingEdit | None":
    new_genre = _normalize(raw)
    if not new_genre:
        return None
    load_album_tags(album)
    edit = PendingEdit(f"Album genre → {new_genre!r}")
    for track in album.children:
        if track.tags:
            edit.tag_writes.append((track.path, {"TCON": new_genre}))
    return edit


def _build_album_artist(album: Node, raw: str) -> "PendingEdit | None":
    """Move album to a different album artist folder and retag ALBUMARTIST."""
    new_artist = _sanitize(raw)
    new_tag    = _normalize(raw)
    if not new_artist:
        return None
    load_album_tags(album)

    # New location: sibling of current album artist folder, same album folder name
    new_artist_dir = album.path.parent.parent / new_artist
    new_album_dir  = new_artist_dir / album.path.name

    edit = PendingEdit(f"Album artist → {new_artist!r}")
    edit.preview_labels[id(album)] = f"{album.label}  [→ {new_artist}]"

    for track in album.children:
        if track.tags:
            edit.tag_writes.append((track.path, {"ALBUMARTIST": new_tag}))

    if new_album_dir != album.path:
        edit.dir_renames.append((album.path, new_album_dir))
    return edit


def _build_track_title(track: Node, raw: str) -> "PendingEdit | None":
    new_title = _normalize(raw)
    if not new_title:
        return None
    t = track.tags
    if not t:
        return None
    num = _track_num(track)
    if num is None:
        return None
    album    = track.parent
    w        = _track_width(album) if album else 2
    artist_s = _sanitize(t.get("artist", ""))
    title_s  = _sanitize(new_title)
    new_fname = _new_track_filename(num, w, artist_s, title_s)
    new_path  = track.path.parent / new_fname
    new_label = _track_label({**t, "title": new_title}, new_path.name)

    edit = PendingEdit(f"Track title → {new_title!r}")
    edit.preview_labels[id(track)] = new_label
    edit.tag_writes.append((track.path, {"TIT2": new_title}))
    if new_path != track.path:
        edit.file_renames.append((track.path, new_path))
    return edit


def _build_track_artist(track: Node, raw: str) -> "PendingEdit | None":
    new_artist = _normalize(raw)
    if not new_artist:
        return None
    t = track.tags
    if not t:
        return None
    num = _track_num(track)
    if num is None:
        return None
    album    = track.parent
    w        = _track_width(album) if album else 2
    artist_s = _sanitize(new_artist)
    title_s  = _sanitize(t.get("title", ""))
    new_fname = _new_track_filename(num, w, artist_s, title_s)
    new_path  = track.path.parent / new_fname
    new_label = _track_label({**t, "artist": new_artist}, new_path.name)

    edit = PendingEdit(f"Track artist → {new_artist!r}")
    edit.preview_labels[id(track)] = new_label
    edit.tag_writes.append((track.path, {"TPE1": new_artist}))
    if new_path != track.path:
        edit.file_renames.append((track.path, new_path))
    return edit


# ── Apply ─────────────────────────────────────────────────────────────────────

def _apply_pending(pending: list[PendingEdit]) -> tuple[bool, str]:
    errors: list[str] = []

    for edit in pending:
        for path, updates in edit.tag_writes:
            try:
                _write_tags(path, updates)
            except Exception as exc:
                errors.append(f"tag:{path.name}: {exc}")

        for old, new in edit.file_renames:
            try:
                if old.exists() and old != new:
                    if new.exists():
                        errors.append(f"exists:{new.name}")
                    else:
                        old.rename(new)
            except Exception as exc:
                errors.append(f"rename:{old.name}: {exc}")

        # Rename dirs deepest-first so children don't invalidate parents
        for old, new in sorted(edit.dir_renames, key=lambda x: -len(x[0].parts)):
            try:
                if old.exists() and old != new:
                    new.parent.mkdir(parents=True, exist_ok=True)
                    if new.exists():
                        errors.append(f"exists:{new.name}")
                    else:
                        shutil.move(str(old), str(new))
            except Exception as exc:
                errors.append(f"move:{old.name}: {exc}")

        # Remove directories emptied by a merge
        for path in edit.dir_removals:
            try:
                if path.exists():
                    path.rmdir()
            except OSError as exc:
                errors.append(f"rmdir:{path.name}: {exc}")

    return (not errors), "  |  ".join(errors)


# ── Public edit API (for server.py) ───────────────────────────────────────────
# Maps an (op, value) request to the matching pure _build_* function and applies
# it via _apply_pending.

_EDIT_BUILDERS = {
    "artist_rename": (ARTIST, _build_artist_rename),
    "artist_genre":  (ARTIST, _build_artist_genre),
    "album_title":   (ALBUM,  _build_album_title),
    "album_year":    (ALBUM,  _build_album_year),
    "album_genre":   (ALBUM,  _build_album_genre),
    "album_artist":  (ALBUM,  _build_album_artist),
    "track_title":   (TRACK,  _build_track_title),
    "track_artist":  (TRACK,  _build_track_artist),
}


def search(root: Path, query: str, limit: int = 20) -> dict:
    """Case-insensitive search of the library for artists, albums and tracks.

    Artists match on the artist name; albums match on the album folder label or
    the artist name; tracks match on the track filename (which encodes
    Artist - Title in a standardized library). Matched tracks are enriched with
    read_tags() for a clean title/artist. Returns
    {"artists": [...], "albums": [...], "tracks": [...]} capped at *limit* each.
    """
    q = query.strip().lower()
    artists: list[dict] = []
    albums: list[dict] = []
    tracks: list[dict] = []
    if not q:
        return {"artists": artists, "albums": albums, "tracks": tracks}

    for artist in build_tree(root):
        artist_match = q in artist.label.lower()
        if artist_match and len(artists) < limit:
            artists.append({
                "artist": artist.label, "artist_path": str(artist.path),
                "n_albums": len(artist.children),
            })
        for album in artist.children:
            if len(albums) < limit and (artist_match or q in album.label.lower()):
                albums.append({
                    "artist": artist.label, "album": album.label,
                    "path": str(album.path), "artist_path": str(artist.path),
                })
            for track in album.children:
                if len(tracks) < limit and q in track.label.lower():
                    tags = read_tags(track.path)
                    tracks.append({
                        "title": tags.get("title") or track.path.stem,
                        "artist": tags.get("artist", ""),
                        "album": album.label,
                        "path": str(track.path),
                        "album_path": str(album.path),
                        "artist_path": str(artist.path),
                    })
    return {"artists": artists, "albums": albums, "tracks": tracks}


def _album_row(artist: "Node", album: "Node", tags: dict) -> dict:
    """A grid row for the Browse UI: album/artist/year + the paths needed to
    reveal it. *tags* are the first track's tags (how an album's metadata is
    derived everywhere)."""
    return {
        "album": tags.get("album") or album.label,
        "artist": tags.get("albumartist") or tags.get("artist") or artist.label,
        "year": tags.get("year", ""),
        "album_path": str(album.path), "artist_path": str(artist.path),
    }


def albums_by_genre(root: Path, genre: str) -> list[dict]:
    """All albums whose genre matches *genre* (case-insensitive, trimmed).

    An album's genre is taken from its first track's TCON, mirroring how the
    Browse UI derives an album's genre. Returns a list of
    {"album", "artist", "year", "album_path", "artist_path"} sorted A-Z by
    album label.
    """
    want = genre.strip().lower()
    out: list[dict] = []
    if not want:
        return out

    for artist in build_tree(root):
        for album in artist.children:
            if not album.children:
                continue
            tags = read_tags(album.children[0].path)
            if (tags.get("genre") or "").strip().lower() != want:
                continue
            out.append(_album_row(artist, album, tags))
    out.sort(key=lambda a: a["album"].lower())
    return out


def all_albums(root: Path) -> list[dict]:
    """Every album in the library, same row shape as albums_by_genre(), sorted
    A-Z by album label. The Browse UI re-sorts client-side (A-Z / date / random)."""
    out: list[dict] = []
    for artist in build_tree(root):
        for album in artist.children:
            if not album.children:
                continue
            out.append(_album_row(artist, album, read_tags(album.children[0].path)))
    out.sort(key=lambda a: a["album"].lower())
    return out


def all_genres(root: Path) -> list[dict]:
    """Every distinct genre in the library with its album count.

    An album's genre is taken from its first track's TCON, mirroring
    albums_by_genre(). Albums with no genre are skipped. Returns a list of
    {"genre", "count"} sorted A-Z by genre (case-insensitive).
    """
    counts: dict[str, int] = {}
    for artist in build_tree(root):
        for album in artist.children:
            if not album.children:
                continue
            genre = (read_tags(album.children[0].path).get("genre") or "").strip()
            if not genre:
                continue
            counts[genre] = counts.get(genre, 0) + 1
    return [{"genre": g, "count": n}
            for g, n in sorted(counts.items(), key=lambda kv: kv[0].lower())]


def library_manifest(root: Path) -> dict:
    """Flat manifest of every track, for an offline-first native client.

    One entry per mp3 carrying: a stable relative-path id (`rel`), the absolute
    path (`path`, for the existing /api/track and /api/cover routes), a cheap
    change token (`sig` = size + mtime, opaque to the client), and the tags
    needed to build the whole library UI without a per-album round trip. The
    client diffs `sig` against its last download to sync only what changed —
    the same signal `sync_library` trusts for device mirroring.

    Built on the cached build_tree + read_tags, so a repeat call after the first
    walk is cheap. `album_rel` groups tracks into albums client-side."""
    tracks: list[dict] = []
    for artist in build_tree(root):
        for album in artist.children:
            for tnode in album.children:
                p = tnode.path
                try:
                    st = p.stat()
                except OSError:
                    continue
                tags = read_tags(p)
                tracks.append({
                    "rel":         str(p.relative_to(root)),
                    "path":        str(p),
                    "album_rel":   str(album.path.relative_to(root)),
                    "size":        st.st_size,
                    "sig":         f"{st.st_size}-{st.st_mtime_ns}",
                    "artist":      tags.get("artist", ""),
                    "albumartist": tags.get("albumartist", ""),
                    "album":       tags.get("album", ""),
                    "title":       tags.get("title", "") or p.stem,
                    "track":       tags.get("track", ""),
                    "year":        tags.get("year", ""),
                    "genre":       tags.get("genre", ""),
                    "duration":    tags.get("length_sec", 0),
                    "bitrate":     tags.get("bitrate", ""),
                })
    return {"root": str(root), "count": len(tracks), "tracks": tracks}


# ── Collections (owner-curated album groups) ──────────────────────────────────
# Collections live in settings ({root}/.mp3tools/mp3tools.conf) as a list of
# {"name", "albums": [ref, ...]}; each ref is {"path", "artist", "album", "year"}
# with *path* relative to the library root. Browsing resolves refs to live album
# nodes — self-healing a renamed folder via the stored metadata — and returns the
# same row shape as albums_by_genre() so the Browse grid is reused verbatim.

def _rel_album_path(root: Path, album_path) -> str:
    """An album folder path relative to *root*, as a POSIX string (portable so a
    collection survives the library being moved). Falls back to the raw path if
    it can't be made relative."""
    p, r = Path(album_path), Path(root)
    try:
        return p.relative_to(r).as_posix()
    except ValueError:
        try:
            return p.resolve().relative_to(r.resolve()).as_posix()
        except ValueError:
            return p.as_posix()


def _album_ref(root: Path, artist: "Node", album: "Node", tags: dict) -> dict:
    """The stored form of an album reference: its relative path plus the metadata
    that backs self-heal. Metadata mirrors _album_row() so a healed match agrees
    with what the grid shows."""
    return {
        "path":   _rel_album_path(root, album.path),
        "artist": tags.get("albumartist") or tags.get("artist") or artist.label,
        "album":  tags.get("album") or album.label,
        "year":   tags.get("year", ""),
    }


def _ck(s: str | None) -> str:
    return (s or "").strip().lower()


def _album_entries(root: Path):
    """One build_tree() pass → (entries, by_path). *entries* is a list of
    (artist, album, tags) for every non-empty album (tags from the first track,
    as everywhere); *by_path* indexes them by relative album path."""
    entries: list[tuple] = []
    by_path: dict[str, tuple] = {}
    for artist in build_tree(root):
        for album in artist.children:
            if not album.children:
                continue
            tags = read_tags(album.children[0].path)
            entry = (artist, album, tags)
            entries.append(entry)
            by_path[_rel_album_path(root, album.path)] = entry
    return entries, by_path


def _match_ref(entries: list, ref: dict):
    """Find the album entry matching a ref's stored artist/album/year (the
    self-heal fallback when its path no longer resolves)."""
    want = (_ck(ref.get("artist")), _ck(ref.get("album")), _ck(ref.get("year")))
    for artist, album, tags in entries:
        got = (_ck(tags.get("albumartist") or tags.get("artist") or artist.label),
               _ck(tags.get("album") or album.label),
               _ck(tags.get("year")))
        if got == want:
            return (artist, album, tags)
    return None


def _find_collection(cfg: dict, name: str) -> dict | None:
    key = _ck(name)
    for coll in cfg.get("collections", []):
        if _ck(coll.get("name")) == key:
            return coll
    return None


def all_collections(root: Path, cfg: dict) -> list[dict]:
    """Every collection with its resolvable-album count, sorted A-Z by name. The
    count reflects only albums that currently resolve (by path or metadata), so a
    stale reference doesn't inflate it."""
    entries, by_path = _album_entries(root)
    out: list[dict] = []
    for coll in cfg.get("collections", []):
        count = sum(1 for ref in coll.get("albums", [])
                    if ref["path"] in by_path or _match_ref(entries, ref) is not None)
        out.append({"name": coll["name"], "count": count})
    out.sort(key=lambda c: c["name"].lower())
    return out


def collection_albums(root: Path, cfg: dict, name: str) -> tuple[list[dict], bool]:
    """Album rows for a collection in stored order, plus a *changed* flag. Refs
    whose folder was renamed are self-healed (matched by metadata, their stored
    path rewritten in *cfg* so the caller can persist); refs that resolve to
    nothing are skipped. Rows share the albums_by_genre() shape."""
    coll = _find_collection(cfg, name)
    if coll is None:
        return [], False
    entries, by_path = _album_entries(root)
    rows: list[dict] = []
    changed = False
    for ref in coll["albums"]:
        entry = by_path.get(ref["path"])
        if entry is None:
            entry = _match_ref(entries, ref)
            if entry is not None:
                ref["path"] = _rel_album_path(root, entry[1].path)
                changed = True
        if entry is None:
            continue
        artist, album, tags = entry
        rows.append(_album_row(artist, album, tags))
    return rows, changed


def create_collection(cfg: dict, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("collection name required")
    cfg.setdefault("collections", [])
    if _find_collection(cfg, name) is not None:
        raise ValueError(f"a collection named {name!r} already exists")
    cfg["collections"].append({"name": name, "albums": []})
    return cfg


def rename_collection(cfg: dict, old: str, new: str) -> dict:
    new = new.strip()
    if not new:
        raise ValueError("collection name required")
    coll = _find_collection(cfg, old)
    if coll is None:
        raise ValueError(f"no collection named {old!r}")
    clash = _find_collection(cfg, new)
    if clash is not None and clash is not coll:
        raise ValueError(f"a collection named {new!r} already exists")
    coll["name"] = new
    return cfg


def delete_collection(cfg: dict, name: str) -> dict:
    coll = _find_collection(cfg, name)
    if coll is None:
        raise ValueError(f"no collection named {name!r}")
    cfg["collections"].remove(coll)
    return cfg


def add_to_collection(root: Path, cfg: dict, name: str, album_path) -> dict:
    """Add the album at *album_path* (absolute) to a collection. Idempotent —
    re-adding the same album is a no-op."""
    coll = _find_collection(cfg, name)
    if coll is None:
        raise ValueError(f"no collection named {name!r}")
    node = find_node(root, Path(album_path))
    if node is None or node.kind != ALBUM or not node.children:
        raise ValueError("album not found")
    rel = _rel_album_path(root, node.path)
    if any(ref["path"] == rel for ref in coll["albums"]):
        return cfg
    tags = read_tags(node.children[0].path)
    coll["albums"].append(_album_ref(root, node.parent, node, tags))
    return cfg


def remove_from_collection(root: Path, cfg: dict, name: str, album_path) -> dict:
    """Remove the album at *album_path* from a collection (no-op if absent)."""
    coll = _find_collection(cfg, name)
    if coll is None:
        raise ValueError(f"no collection named {name!r}")
    rel = _rel_album_path(root, album_path)
    coll["albums"] = [ref for ref in coll["albums"] if ref["path"] != rel]
    return cfg


def merge_genres(root: Path, src: str, dst: str) -> tuple[bool, str, int]:
    """Re-tag every album whose genre is *src* to *dst* (case-insensitive match).

    Reuses the album_genre edit builder + apply_edits — no new tag logic.
    Returns (ok, error_string, n_albums_changed).
    """
    edits: list[PendingEdit] = []
    for a in albums_by_genre(root, src):
        edit = build_edit(root, Path(a["album_path"]), "album_genre", dst)
        if edit:
            edits.append(edit)
    if not edits:
        return True, "", 0
    ok, err = apply_edits(edits)
    return ok, err, len(edits)


def find_node(root: Path, path: Path) -> "Node | None":
    """Locate the Node whose .path == *path* within build_tree(root)."""
    path = Path(path)
    stack = list(build_tree(root))
    while stack:
        node = stack.pop()
        if node.path == path:
            return node
        stack.extend(node.children)
    return None


def build_edit(root: Path, node_path: Path, op: str, value: str) -> "PendingEdit | None":
    """Build a PendingEdit for *op* on the node at *node_path* (nothing written)."""
    spec = _EDIT_BUILDERS.get(op)
    if spec is None:
        raise ValueError(f"unknown edit op: {op!r}")
    expected_kind, builder = spec
    node = find_node(root, node_path)
    if node is None or node.kind != expected_kind:
        return None
    if node.kind == TRACK and not node.tags and node.parent:
        load_album_tags(node.parent)
    return builder(node, value)


def invalidate_caches() -> None:
    """Drop the tree/tag caches. The mtime signatures already invalidate on any
    filesystem change; this is a belt-and-braces hook for the app's own mutation
    paths so an edit is never masked by timestamp granularity."""
    _TREE_CACHE.clear()
    _TAG_CACHE.clear()


def apply_edits(edits: "list[PendingEdit]") -> tuple[bool, str]:
    """Apply a list of PendingEdits to disk. Returns (ok, error_string)."""
    try:
        return _apply_pending(edits)
    finally:
        invalidate_caches()


def reorder_album(album_dir: Path, ordered_paths: "list[Path]") -> tuple[bool, str]:
    """Renumber an album's tracks to the given order: write TRCK = i/N and rename
    files to 'NN. Artist - Title.mp3'. Two-phase rename (via temp names) so reorders
    that swap numbers don't collide. Returns (ok, error_string)."""
    current = sorted(album_dir.glob("*.mp3"))
    ordered = [Path(p) for p in ordered_paths]
    if set(ordered) != set(current):
        return False, "track set does not match the album"

    n = len(ordered)
    width = 3 if n >= 100 else 2
    errors: list[str] = []

    # Write TRCK first (files still at their current paths), then collision-safe rename.
    plan: list[tuple[Path, Path]] = []   # (current_path, final_path)
    for i, path in enumerate(ordered, 1):
        tags = read_tags(path) or {}
        artist_s = _sanitize(tags.get("artist", ""))
        title_s = _sanitize(tags.get("title", "") or path.stem)
        final = album_dir / _new_track_filename(i, width, artist_s, title_s)
        try:
            _write_tags(path, {"TRCK": f"{str(i).zfill(width)}/{n}"})
        except Exception as exc:
            errors.append(f"tag:{path.name}: {exc}")
        plan.append((path, final))

    temps: list[tuple[Path, Path]] = []
    try:
        for idx, (path, final) in enumerate(plan):
            tmp = album_dir / f".reorder-{idx}.tmp"
            path.rename(tmp)
            temps.append((tmp, final))
        for tmp, final in temps:
            if final.exists() and final != tmp:
                errors.append(f"exists:{final.name}")
            else:
                tmp.rename(final)
    except Exception as exc:
        errors.append(f"rename: {exc}")
    finally:
        invalidate_caches()

    return (not errors), "  |  ".join(errors)


# ── Online art fetch ─────────────────────────────────────────────────────────

def _album_search_terms(node: Node) -> tuple[str, str]:
    """Return (artist, album_title) for iTunes search from an ALBUM node."""
    artist = node.parent.label if node.parent else ""
    album  = node.label
    m = _ALBUM_YEAR_RE.match(album)
    if m:
        album = m.group(1)
    # Prefer tag values if available
    if node.children:
        first = node.children[0]
        if first.tags:
            if first.tags.get("albumartist"):
                artist = first.tags["albumartist"]
            elif first.tags.get("artist"):
                artist = first.tags["artist"]
            if first.tags.get("album"):
                album = first.tags["album"]
        else:
            try:
                t = _load_id3(first.path)
                aa = _album_artist_value(t)
                if aa:
                    artist = aa
                talb = t.get("TALB")
                if talb:
                    album = str(talb.text[0])
            except Exception:
                pass
    return artist, album


def _apply_art_to_album(album: Node, data: bytes, mime: str,
                         cover_art: str, cover_art_size: int) -> tuple[int, int]:
    """Write/embed art for *album*. Returns (updated, errors)."""
    data, mime = resize_artwork(data, mime, cover_art_size)
    updated = errors = 0

    if cover_art in ("folder", "both"):
        ext = ".jpg" if ("jpeg" in mime or "jpg" in mime) else ".png"
        cover_path = album.path / f"cover{ext}"
        try:
            for existing in sorted(album.path.iterdir()):
                if (existing.is_file()
                        and existing.suffix.lower() in _IMAGE_EXTENSIONS
                        and existing != cover_path):
                    existing.unlink()
            cover_path.write_bytes(data)
            updated += 1
        except Exception:
            errors += 1

    if cover_art in ("embed", "both"):
        for mp3 in sorted(album.path.glob("*.mp3")):
            try:
                tags = _load_id3(mp3)
                tags["APIC:"] = _APIC(encoding=3, mime=mime, type=3, desc="", data=data)
                tags.save(mp3, v2_version=3, v1=0)
                updated += 1
            except Exception:
                errors += 1

    return updated, errors


def _remove_art_from_album(album: Node, mode: str) -> tuple[int, int]:
    """Remove folder images and/or embedded APIC art. Returns (removed, errors)."""
    removed = errors = 0

    if mode in ("folder", "both"):
        for image in sorted(album.path.iterdir()):
            if not image.is_file() or image.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            try:
                image.unlink()
                removed += 1
            except Exception:
                errors += 1

    if mode in ("embed", "both"):
        for mp3 in sorted(album.path.glob("*.mp3")):
            try:
                tags = _load_id3(mp3)
            except ID3NoHeaderError:
                continue
            except Exception:
                errors += 1
                continue

            if not tags.getall("APIC"):
                continue

            try:
                tags.delall("APIC")
                tags.save(mp3, v2_version=3, v1=0)
                removed += 1
            except Exception:
                errors += 1

    return removed, errors
