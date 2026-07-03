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
    tags["TPE2"] = _TPE2(encoding=3, text=value)


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
    return [Node(TRACK, mp3.name, mp3, parent=parent) for mp3 in mp3s]


def build_tree(root: Path) -> list[Node]:
    """
    Build a Node tree from *root*, auto-detecting which level it represents.

    Library root  root/Album Artist/Album/mp3   → 3-level tree
    Album artist dir root/Album/mp3             → 2-level tree
    Album dir     root/mp3                      → 1-level
    """
    child_dirs = _subdirs(root)

    direct = _mp3s(root)
    if direct:
        artist = Node(ARTIST, root.name, root)
        album  = Node(ALBUM,  root.name, root, parent=artist)
        album.children = _make_tracks(direct, album)
        artist.children = [album]
        return [artist]

    if any(_mp3s(d) for d in child_dirs):
        artist = Node(ARTIST, root.name, root)
        for album_dir in child_dirs:
            mp3s = _mp3s(album_dir)
            if not mp3s:
                continue
            album = Node(ALBUM, album_dir.name, album_dir, parent=artist)
            album.children = _make_tracks(mp3s, album)
            artist.children.append(album)
        return [artist] if artist.children else []

    artists: list[Node] = []
    for artist_dir in child_dirs:
        artist = Node(ARTIST, artist_dir.name, artist_dir)
        for album_dir in _subdirs(artist_dir):
            mp3s = _mp3s(album_dir)
            if not mp3s:
                continue
            album = Node(ALBUM, album_dir.name, album_dir, parent=artist)
            album.children = _make_tracks(mp3s, album)
            artist.children.append(album)
        if artist.children:
            artists.append(artist)
    return artists


# ── Tag I/O ───────────────────────────────────────────────────────────────────

def _fmt_dur(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _read_tags(path: Path) -> dict[str, str]:
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
            tags[frame_id] = cls(encoding=3, text=value)
    tags.save(path, v2_version=3, v1=0)


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


def apply_edits(edits: "list[PendingEdit]") -> tuple[bool, str]:
    """Apply a list of PendingEdits to disk. Returns (ok, error_string)."""
    return _apply_pending(edits)


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
