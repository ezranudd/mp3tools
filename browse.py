#!/usr/bin/env python3
"""
Terminal music library browser with edit mode.

Expected structure: root/Album Artist/Album/mp3s
Auto-detects if you point it at the library root, an artist folder, or an album folder.

Browse controls
  ↑ / ↓ / j / k    Navigate
  → / Enter / Space Expand / collapse
  ←                 Collapse node or jump to parent
  PgUp / PgDn       Scroll one page
  g / Home          Jump to top
  G / End           Jump to bottom
  e                 Edit selected node
  r                 Fetch online album art for selected album/artist
  x                 Remove album art from selected album
  q / Esc           Quit

Edit / preview controls
  e (on artist)     Edit album artist or genre for all albums
  e (on album)      Edit title, year, album artist, or genre
  e (on track)      Edit track title or artist
  a                 Apply all pending edits
  Esc               Discard pending edits and return to browse
"""

import argparse
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

import settings as settings_mod
from fetch_art import (
    CONFIDENT_MATCH_SCORE,
    fetch_artwork,
    resize_artwork,
    search_art_sources,
)
from termtext import cell_width, clip_cells, fit_cells
from chars import CHAR_REPLACEMENTS as _CHAR_MAP

os.environ.setdefault("ESCDELAY", "25")

import curses

from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    APIC as _APIC,
    TPE1 as _TPE1, TPE2 as _TPE2, TIT2 as _TIT2, TALB as _TALB,
    TYER as _TYER, TDRC as _TDRC, TCON as _TCON, TRCK as _TRCK,
    TXXX as _TXXX,
)


# ── Character normalization ───────────────────────────────────────────────────

_YEAR_RE       = re.compile(r"\b(19\d{2}|20\d{2})\b")
_ALBUM_YEAR_RE = re.compile(r"^\d{4}\s*-\s*(.+)$")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_ALBUM_ARTIST_DESC = "album artist"
_ALBUM_ARTIST_KEYS = (
    "TXXX:album artist",
    "TXXX:ALBUMARTIST",
    "TXXX:ALBUM ARTIST",
    "TXXX:AlbumArtist",
    "TXXX:Album Artist",
    "TPE2",
)


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    for old, new in _CHAR_MAP.items():
        s = s.replace(old, new)
    return s


def _sanitize(s: str) -> str:
    s = _normalize(s)
    for old, new in {"/": "-", "\\": "-", ":": " -", "*": "",
                     "?": "", '"': "'", "<": "", ">": "", "|": "-"}.items():
        s = s.replace(old, new)
    return s.rstrip(". ")


def _extract_year(s: str) -> str | None:
    m = _YEAR_RE.search(s)
    return m.group(1) if m else None


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
    canonical_key = f"TXXX:{_ALBUM_ARTIST_DESC}"
    for key in _ALBUM_ARTIST_KEYS:
        if key not in (canonical_key, "TPE2") and key in tags:
            del tags[key]
    tags["TPE2"] = _TPE2(encoding=3, text=value)
    tags[canonical_key] = _TXXX(
        encoding=3,
        desc=_ALBUM_ARTIST_DESC,
        text=value,
    )


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


# ── Color pairs & UI primitives ───────────────────────────────────────────────
# Canonical palette and drawing/input primitives live in ui.py; imported here
# (and re-exported) so this module and its callers share one implementation.

from ui import (
    C_ARTIST, C_ALBUM, C_TRACK, C_HDR, C_BAR, C_DIM, C_EDIT,
    init_colors as _init_colors,
    put as _put, text_input as _text_input, choose as _choose,
)


def _draw(stdscr, items: list[Node], sel: int, scroll: int, root_str: str,
          preview_labels: "dict[int, str]", in_preview: bool,
          flash: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    list_h = max(1, h - 2)

    # ── Header bar ────────────────────────────────────────────────────────────
    if in_preview:
        keys = " Preview  a=Apply  e=Edit more  Esc=Discard "
    else:
        keys = " j/k=Move  g/G=Top/Bottom  →/Enter=Expand  ←=Collapse  e=Edit  r=Art  x=RemoveArt  q=Back "
    path_str = f" {root_str}"
    gap    = max(0, w - cell_width(keys))
    header = fit_cells(path_str, gap) + keys
    _put(stdscr, 0, 0, clip_cells(header, w), curses.color_pair(C_HDR) | curses.A_BOLD)

    if not items:
        _put(stdscr, 2, 2, "No music found.", curses.A_DIM)
        stdscr.refresh()
        return

    # ── Tree rows ─────────────────────────────────────────────────────────────
    for i, node in enumerate(items[scroll : scroll + list_h]):
        row      = i + 1
        selected = (i + scroll) == sel
        edited   = id(node) in preview_labels
        disp     = preview_labels.get(id(node), node.label)

        if node.kind == ARTIST:
            arrow = "▼ " if node.expanded else "▶ "
            label = arrow + disp
            na, nt = _n_albums(node), _n_tracks(node)
            aside = (f"  {na:>3} album{'s' if na != 1 else ' '}"
                     f"  {nt:>4} track{'s' if nt != 1 else ' '}")
            base  = curses.color_pair(C_EDIT if edited else C_ARTIST) | curses.A_BOLD
        elif node.kind == ALBUM:
            arrow = "▼ " if node.expanded else "▶ "
            label = "  " + arrow + disp
            nt    = _n_tracks(node)
            aside = f"  {nt:>4} track{'s' if nt != 1 else ' '}"
            base  = curses.color_pair(C_EDIT if edited else C_ALBUM)
        else:
            label = "      " + disp
            aside = ""
            base  = curses.color_pair(C_EDIT if edited else C_TRACK)
            if edited:
                base |= curses.A_BOLD

        aside_w = cell_width(aside)
        label_w = max(0, w - aside_w - 1)
        label_s = fit_cells(label, label_w)

        if selected:
            full = fit_cells(label_s + aside, w - 1)
            _put(stdscr, row, 0, full, curses.A_REVERSE | curses.A_BOLD)
        else:
            _put(stdscr, row, 0, label_s, base)
            if aside:
                _put(stdscr, row, label_w, clip_cells(aside, w - label_w - 1),
                     curses.color_pair(C_DIM) | curses.A_DIM)

    # ── Status bar ────────────────────────────────────────────────────────────
    if flash:
        info = " " + flash
    else:
        node = items[sel]
        if node.kind == TRACK:
            t = node.tags
            if t:
                raw_trk = t.get("track", "").split("/")[0].strip()
                parts   = [t.get("title") or node.path.stem]
                if t.get("artist"): parts.append(t["artist"])
                if t.get("albumartist"): parts.append(t["albumartist"])
                if t.get("album"):  parts.append(t["album"])
                if t.get("year"):   parts.append(t["year"])
                if raw_trk:         parts.append(f"Track {raw_trk}")
                if t.get("genre"):   parts.append(t["genre"])
                if t.get("bitrate"): parts.append(f"{t['bitrate']} kbps")
                info = " " + "  │  ".join(parts)
            else:
                info = f" {node.path.stem}"
        elif node.kind == ALBUM:
            nt     = _n_tracks(node)
            parent = node.parent.label if node.parent else ""
            info   = f" {node.label}  │  {parent}  │  {nt} track{'s' if nt != 1 else ''}"
        else:
            na, nt = _n_albums(node), _n_tracks(node)
            info   = (f" {node.label}  │  {na} album{'s' if na != 1 else ''}"
                      f"  │  {nt} track{'s' if nt != 1 else ''}")

    _put(stdscr, h - 1, 0, fit_cells(info, w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()


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


# ── Public edit API (curses-free; for server.py) ──────────────────────────────
# Maps an (op, value) request to the matching pure _build_* function and applies
# it via _apply_pending. The TUI's _do_edit dispatcher stays as the curses path.

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
            out.append({
                "album": tags.get("album") or album.label,
                "artist": tags.get("albumartist") or tags.get("artist") or artist.label,
                "year": tags.get("year", ""),
                "album_path": str(album.path), "artist_path": str(artist.path),
            })
    out.sort(key=lambda a: a["album"].lower())
    return out


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


# ── Edit dispatcher ───────────────────────────────────────────────────────────

def _do_edit(stdscr, node: Node) -> "PendingEdit | None":
    h, _ = stdscr.getmaxyx()
    bar   = h - 1

    if node.kind == TRACK:
        if not node.tags:
            load_album_tags(node.parent)
        choice = _choose(stdscr, bar, "Edit track",
                         [("t", "Title"), ("a", "Artist")])
        if not choice:
            return None

        if choice == "t":
            cur = node.tags.get("title", node.path.stem)
            val = _text_input(stdscr, bar, f" Title [{cur}]: ", cur)
            return _build_track_title(node, val) if val else None

        if choice == "a":
            cur = node.tags.get("artist", "")
            val = _text_input(stdscr, bar, f" Artist [{cur}]: ", cur)
            return _build_track_artist(node, val) if val else None

    elif node.kind == ALBUM:
        choice = _choose(stdscr, bar, "Edit album",
                         [("t", "Title"), ("y", "Year"), ("a", "Album Artist"), ("g", "Genre")])
        if not choice:
            return None
        load_album_tags(node)

        if choice == "t":
            cur = next((tr.tags.get("album", "") for tr in node.children if tr.tags.get("album")), "")
            if not cur:
                m = re.match(r"^\d{4} - (.+)$", node.label)
                cur = m.group(1) if m else node.label
            val = _text_input(stdscr, bar, f" Album title [{cur}]: ", cur)
            return _build_album_title(node, val) if val else None

        elif choice == "y":
            cur_y = ""
            for tr in node.children:
                y = tr.tags.get("year", "")
                if y:
                    cur_y = _extract_year(y) or y
                    break
            val = _text_input(stdscr, bar, f" Year [{cur_y}]: ", cur_y)
            return _build_album_year(node, val) if val else None

        elif choice == "a":
            cur_a = next(
                (tr.tags.get("albumartist", "") or tr.tags.get("artist", "")
                 for tr in node.children
                 if tr.tags.get("albumartist") or tr.tags.get("artist")),
                "",
            )
            val = _text_input(stdscr, bar, f" Album artist [{cur_a}]: ", cur_a)
            return _build_album_artist(node, val) if val else None

        elif choice == "g":
            cur_g = next((tr.tags.get("genre", "") for tr in node.children if tr.tags.get("genre")), "")
            val = _text_input(stdscr, bar, f" Genre [{cur_g}]: ", cur_g)
            return _build_album_genre(node, val) if val else None

    elif node.kind == ARTIST:
        choice = _choose(stdscr, bar, "Edit artist",
                         [("n", "Album Artist"), ("g", "Genre")])
        if not choice:
            return None

        if choice == "n":
            val = _text_input(stdscr, bar, f" Album artist [{node.label}]: ", node.label)
            return _build_artist_rename(node, val) if val else None

        elif choice == "g":
            cur_g = ""
            for album in node.children:
                load_album_tags(album)
                cur_g = next((tr.tags.get("genre", "") for tr in album.children if tr.tags.get("genre")), "")
                if cur_g:
                    break
            val = _text_input(stdscr, bar, f" Genre [{cur_g}]: ", cur_g)
            return _build_artist_genre(node, val) if val else None

    return None


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


def _pick_artwork(stdscr, results: list[dict], label: str) -> int:
    """Overlay showing search results. Returns selected index or -1 for cancel."""
    n = min(len(results), 9)

    def draw_picker() -> None:
        h, w = stdscr.getmaxyx()
        if h < 4 or w < 20:
            stdscr.erase()
            _put(stdscr, 0, 0, "Resize terminal larger.", curses.color_pair(C_BAR))
            stdscr.refresh()
            return

        start = max(1, h - n - 3)
        for row in range(start, h):
            _put(stdscr, row, 0, " " * max(0, w - 1), curses.A_NORMAL)

        _put(stdscr, start, 0,
             fit_cells(f" Artwork results for {label!r}", w - 1),
             curses.color_pair(C_HDR) | curses.A_BOLD)

        for i, res in enumerate(results[:n]):
            source = res.get("source_label", res.get("source", ""))
            artist = res.get("artist", "")
            album  = res.get("album", "")
            year   = res.get("year", "")
            size   = res.get("size", "")
            line   = f"  [{i + 1}] {source:<11} {artist} - {album}"
            if year:
                line += f"  ({year})"
            if size:
                line += f"  [{size}]"
            _put(stdscr, start + 1 + i, 0, fit_cells(line, w - 1), curses.A_NORMAL)

        _put(stdscr, h - 1, 0,
             fit_cells(f" 1-{n}=Select  Esc=Cancel", w - 1),
             curses.color_pair(C_BAR))
        stdscr.refresh()

    draw_picker()

    while True:
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        if isinstance(key, str):
            if key == "\x1b":
                return -1
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < n:
                    return idx
        elif key == 27:
            return -1
        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            draw_picker()


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


def _remove_art_for_album(stdscr, album: Node) -> str:
    """Interactive album-art removal for one album."""
    h, _ = stdscr.getmaxyx()
    choice = _choose(
        stdscr, h - 1, f"Remove art from {album.label}",
        [("f", "Folder files"), ("e", "Embedded tags"), ("b", "Both")],
    )
    if not choice:
        return ""

    mode = {"f": "folder", "e": "embed", "b": "both"}[choice]
    removed, errors = _remove_art_from_album(album, mode)
    if errors:
        return f"Art removal: {removed} removed, {errors} errors"
    if removed:
        return f"Removed art from {album.label}"
    return f"No art found in {album.label}"


def _fetch_art_for_album(stdscr, album: Node, settings: dict, cover_art: str,
                          cover_art_size: int) -> str:
    """Interactive art fetch for one album. Returns a flash message string."""
    h, w = stdscr.getmaxyx()
    artist, album_title = _album_search_terms(album)
    label = f"{artist} - {album_title}".strip(" -") if artist else album_title

    _put(stdscr, h - 1, 0,
         fit_cells(f" Searching artwork sources for {label!r}...", w - 1),
         curses.color_pair(C_BAR))
    stdscr.refresh()

    try:
        results = [
            r for r in search_art_sources(
                artist, album_title, settings,
                interactive=True,
            )
            if r.get("url")
        ]
    except RuntimeError as e:
        return f"Search error: {e}"

    if not results:
        return f"No results for {label!r}"

    idx = _pick_artwork(stdscr, results, label)
    if idx < 0:
        return ""

    _put(stdscr, h - 1, 0, fit_cells(" Downloading...", w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()

    try:
        data, mime = fetch_artwork(results[idx]["url"])
    except RuntimeError as e:
        return f"Download error: {e}"

    updated, errors = _apply_art_to_album(album, data, mime, cover_art, cover_art_size)
    if errors:
        return f"Errors applying art ({errors} failed, {updated} OK)"
    return f"Art applied to {album.label}"


def _fetch_art_for_artist(stdscr, artist: Node, settings: dict, cover_art: str,
                           cover_art_size: int) -> str:
    """Batch-fetch first confident result for each album under *artist*."""
    h, w = stdscr.getmaxyx()
    albums  = artist.children
    total   = len(albums)
    fetched = not_found = uncertain = errors = 0
    by_source: dict[str, int] = {}

    for i, album in enumerate(albums):
        art_str, alb_str = _album_search_terms(album)
        label = f"{art_str} - {alb_str}".strip(" -") if art_str else alb_str

        _put(stdscr, h - 1, 0,
             fit_cells(f" [{i + 1}/{total}] {label}...", w - 1),
             curses.color_pair(C_BAR))
        stdscr.refresh()

        try:
            results = [
                r for r in search_art_sources(
                    art_str, alb_str, settings,
                    interactive=False,
                )
                if r.get("url")
            ]
        except RuntimeError:
            errors += 1
            continue

        if not results:
            not_found += 1
            continue
        if results[0].get("score", 0) < CONFIDENT_MATCH_SCORE:
            uncertain += 1
            continue

        try:
            data, mime = fetch_artwork(results[0]["url"])
        except RuntimeError:
            errors += 1
            continue

        _, errs = _apply_art_to_album(album, data, mime, cover_art, cover_art_size)
        if errs:
            errors += errs
        else:
            fetched += 1
            source = results[0].get("source_label", results[0].get("source", "source"))
            by_source[source] = by_source.get(source, 0) + 1

    parts = []
    if fetched:    parts.append(f"{fetched} fetched")
    for source, count in sorted(by_source.items()):
        parts.append(f"{source}: {count}")
    if not_found:  parts.append(f"{not_found} not found")
    if uncertain:  parts.append(f"{uncertain} uncertain")
    if errors:     parts.append(f"{errors} errors")
    return "Art: " + ", ".join(parts) if parts else "Done"


# ── Event loop ────────────────────────────────────────────────────────────────

def _expand(node: Node, artists: list[Node], sel: int) -> int:
    node.expanded = True
    if node.kind == ALBUM:
        load_album_tags(node)
    if node.children:
        return sel + 1
    return sel


def _run(stdscr, artists: list[Node], root: Path, root_str: str) -> None:
    _init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)

    sett           = settings_mod.load(root)
    cover_art      = sett["cover_art"]
    cover_art_size = sett["cover_art_embed_size"]

    sel    = 0
    scroll = 0
    pending:        list[PendingEdit] = []
    preview_labels: dict[int, str]   = {}
    flash = ""

    while True:
        items = visible(artists)
        total = len(items)

        if not total:
            stdscr.erase()
            _put(stdscr, 1, 2, "No music found in this directory.")
            stdscr.refresh()
            if stdscr.getch() in (ord("q"), ord("Q"), 27):
                break
            continue

        sel    = max(0, min(sel, total - 1))
        h, _   = stdscr.getmaxyx()
        list_h = max(1, h - 2)

        if sel < scroll:
            scroll = sel
        elif sel >= scroll + list_h:
            scroll = sel - list_h + 1
        scroll = max(0, scroll)

        in_preview = bool(pending)
        _draw(stdscr, items, sel, scroll, root_str, preview_labels, in_preview, flash)
        flash = ""

        key = stdscr.getch()

        # ── Quit ──────────────────────────────────────────────────────────────
        if key in (ord("q"), ord("Q")):
            break

        # ── Esc: discard pending or quit ──────────────────────────────────────
        elif key == 27:
            if pending:
                pending.clear()
                preview_labels.clear()
            else:
                break

        # ── Navigation ────────────────────────────────────────────────────────
        elif key in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)

        elif key in (curses.KEY_DOWN, ord("j")):
            sel = min(total - 1, sel + 1)

        elif key == curses.KEY_PPAGE:
            sel = max(0, sel - list_h)

        elif key == curses.KEY_NPAGE:
            sel = min(total - 1, sel + list_h)

        elif key in (ord("g"), curses.KEY_HOME):
            sel = 0

        elif key in (ord("G"), curses.KEY_END):
            sel = total - 1

        # ── Expand / collapse ─────────────────────────────────────────────────
        elif key in (ord(" "), ord("\n"), 10, 13):
            node = items[sel]
            if node.kind != TRACK:
                if node.expanded:
                    node.expanded = False
                else:
                    sel = _expand(node, artists, sel)

        elif key in (curses.KEY_RIGHT, ord("l")):
            node = items[sel]
            if node.kind != TRACK:
                if not node.expanded:
                    sel = _expand(node, artists, sel)
                elif sel + 1 < total and items[sel + 1].parent is node:
                    sel += 1

        elif key in (curses.KEY_LEFT, ord("h")):
            node = items[sel]
            if node.kind in (ARTIST, ALBUM) and node.expanded:
                node.expanded = False
            elif node.parent is not None:
                node.parent.expanded = False
                new_items = visible(artists)
                try:
                    sel = new_items.index(node.parent)
                except ValueError:
                    sel = 0

        # ── Edit ──────────────────────────────────────────────────────────────
        elif key == ord("e"):
            edit = _do_edit(stdscr, items[sel])
            if edit:
                pending.append(edit)
                preview_labels.update(edit.preview_labels)

        # ── Apply ─────────────────────────────────────────────────────────────
        elif key in (ord("a"), ord("A")) and pending:
            ok, err = _apply_pending(pending)
            pending.clear()
            preview_labels.clear()
            if ok:
                artists = build_tree(root)
                sel = 0
                scroll = 0
                flash = "Changes applied."
            else:
                artists = build_tree(root)
                sel = 0
                scroll = 0
                flash = f"Errors: {err}"

        # ── Fetch art ─────────────────────────────────────────────────────────
        elif key == ord("r"):
            if pending:
                flash = "Apply or discard pending edits before fetching art."
            else:
                node = items[sel]
                if node.kind == TRACK:
                    node = node.parent
                if node is None:
                    pass
                elif node.kind == ALBUM:
                    flash = _fetch_art_for_album(stdscr, node, sett, cover_art, cover_art_size)
                elif node.kind == ARTIST:
                    flash = _fetch_art_for_artist(stdscr, node, sett, cover_art, cover_art_size)

        # ── Remove art ────────────────────────────────────────────────────────
        elif key == ord("x"):
            if pending:
                flash = "Apply or discard pending edits before removing art."
            else:
                node = items[sel]
                if node.kind == TRACK:
                    node = node.parent
                if node is None:
                    pass
                elif node.kind == ALBUM:
                    flash = _remove_art_for_album(stdscr, node)
                else:
                    flash = "Select an album or track to remove album art."

        # ── Resize ────────────────────────────────────────────────────────────
        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Browse and edit a music library in the terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python browse.py ~/Music               # library root (Album Artist/Album/mp3)
  python browse.py ~/Music/Johnny\\ Paycheck  # single artist
  python browse.py .                     # current directory
        """,
    )
    parser.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Music directory to browse (default: current directory)",
    )
    args = parser.parse_args()

    root = args.directory.resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {root} ...", end="\r", flush=True)
    artists = build_tree(root)

    if not artists:
        print(f"No music found in: {root}")
        sys.exit(0)

    try:
        curses.wrapper(_run, artists, root, str(root))
    except KeyboardInterrupt:
        pass


def run_in_session(stdscr, root: Path) -> None:
    """Enter the browse view using an already-active curses session."""
    artists = build_tree(root)
    if not artists:
        return
    _init_colors()
    _run(stdscr, artists, root, str(root))


if __name__ == "__main__":
    main()
