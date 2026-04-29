#!/usr/bin/env python3
"""
Terminal music library browser with edit mode.

Expected structure: root/Artist/Album/mp3s
Auto-detects if you point it at the library root, an artist folder, or an album folder.

Browse controls
  ↑ / ↓ / j / k    Navigate
  → / Enter / Space Expand / collapse
  ←                 Collapse node or jump to parent
  PgUp / PgDn       Scroll one page
  g / Home          Jump to top
  G / End           Jump to bottom
  e                 Edit selected node
  q / Esc           Quit

Edit / preview controls
  e (on artist)     Edit name or genre for all albums
  e (on album)      Edit title, year, artist, or genre
  e (on track)      Edit track title
  a                 Apply all pending edits
  Esc               Discard pending edits and return to browse
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

os.environ.setdefault("ESCDELAY", "25")

import curses

from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TPE1 as _TPE1, TIT2 as _TIT2, TALB as _TALB,
    TYER as _TYER, TDRC as _TDRC, TCON as _TCON, TRCK as _TRCK,
)


# ── Character normalization ───────────────────────────────────────────────────

_CHAR_MAP: dict[str, str] = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"',
    "–": "-", "—": "-", "−": "-",
    "‐": "-", "‑": "-", "⁃": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", "​": "",
    "×": "x", "⁄": "/", "∕": "/",
    "№": "No.", "℗": "(P)", "℃": "C", "℉": "F",
    "™": "", "®": "", "©": "(C)",
    "•": "-", "·": "-", "†": "+", "‡": "++",
    "′": "'", "″": '"', "‴": "'''", "⁊": "&",
}

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _normalize(s: str) -> str:
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

    Library root  root/Artist/Album/mp3   → 3-level tree
    Artist dir    root/Album/mp3          → 2-level tree (root becomes the artist)
    Album dir     root/mp3               → 1-level (root is artist+album)
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

def _read_tags(path: Path) -> dict[str, str]:
    try:
        audio = MP3(path)
        t = audio.tags
        def g(k: str) -> str:
            if t is None:
                return ""
            f = t.get(k)
            return str(f.text[0]) if f and hasattr(f, "text") else ""
        result = {
            "title":  g("TIT2"),
            "artist": g("TPE1"),
            "album":  g("TALB"),
            "year":   g("TYER") or g("TDRC"),
            "genre":  g("TCON"),
            "track":  g("TRCK"),
        }
        if audio.info:
            result["bitrate"] = str(int(audio.info.bitrate / 1000))
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
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for frame_id, value in updates.items():
        cls = _CLS.get(frame_id)
        if cls:
            tags[frame_id] = cls(encoding=3, text=value)
    tags.save(path, v2_version=3, v1=0)


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


# ── Color pairs ───────────────────────────────────────────────────────────────

C_ARTIST = 1   # bold yellow
C_ALBUM  = 2   # cyan
C_TRACK  = 3   # default fg
C_HDR    = 4   # white on blue   (header bar)
C_BAR    = 5   # black on cyan   (status bar)
C_DIM    = 6   # dim white       (aside counts)
C_EDIT   = 7   # magenta         (pending-edit preview nodes)


def _init_colors() -> None:
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_ARTIST, curses.COLOR_YELLOW,  -1)
        curses.init_pair(C_ALBUM,  curses.COLOR_CYAN,    -1)
        curses.init_pair(C_TRACK,  -1,                   -1)
        curses.init_pair(C_HDR,    curses.COLOR_WHITE,   curses.COLOR_BLUE)
        curses.init_pair(C_BAR,    curses.COLOR_BLACK,   curses.COLOR_CYAN)
        curses.init_pair(C_DIM,    curses.COLOR_WHITE,   -1)
        curses.init_pair(C_EDIT,   curses.COLOR_MAGENTA, -1)
    except curses.error:
        pass


# ── Drawing ───────────────────────────────────────────────────────────────────

def _put(win, y: int, x: int, s: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


def _draw(stdscr, items: list[Node], sel: int, scroll: int, root_str: str,
          preview_labels: "dict[int, str]", in_preview: bool,
          flash: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    list_h = max(1, h - 2)

    # ── Header bar ────────────────────────────────────────────────────────────
    if in_preview:
        keys = " [PREVIEW]  a=Apply  e=Edit more  Esc=Discard "
    else:
        keys = " ↑↓ j/k  PgUp/PgDn  g/G  →/Enter Expand  ← Collapse  e Edit  q Quit "
    path_str = f" {root_str}"
    gap    = max(0, w - len(keys))
    header = path_str[:gap].ljust(gap) + keys
    _put(stdscr, 0, 0, header[:w], curses.color_pair(C_HDR) | curses.A_BOLD)

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

        aside_w = len(aside)
        label_w = max(0, w - aside_w - 1)
        label_s = label[:label_w].ljust(label_w)

        if selected:
            full = (label_s + aside)[: w - 1].ljust(w - 1)
            _put(stdscr, row, 0, full, curses.A_REVERSE | curses.A_BOLD)
        else:
            _put(stdscr, row, 0, label_s, base)
            if aside:
                _put(stdscr, row, label_w, aside[: w - label_w - 1],
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

    _put(stdscr, h - 1, 0, info[: w - 1].ljust(w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()


# ── Text-input widgets ────────────────────────────────────────────────────────

def _text_input(stdscr, row: int, prompt: str, prefill: str = "") -> "str | None":
    """Inline single-line editor on *row*. Returns stripped text or None on Esc."""
    curses.curs_set(1)
    _, w = stdscr.getmaxyx()
    buf = list(prefill)
    pos = len(buf)
    pw  = len(prompt)

    while True:
        line = (prompt + "".join(buf))[:w - 1]
        _put(stdscr, row, 0, line.ljust(w - 1), curses.A_REVERSE)
        try:
            stdscr.move(row, min(pw + pos, w - 2))
        except curses.error:
            pass
        stdscr.refresh()

        try:
            key = stdscr.get_wch()
        except curses.error:
            continue

        if isinstance(key, str):
            if key in ("\n", "\r"):
                break
            if key == "\x1b":
                curses.curs_set(0)
                return None
            if key in ("\x7f", "\b"):
                if pos > 0:
                    buf.pop(pos - 1)
                    pos -= 1
            elif ord(key) >= 32:
                buf.insert(pos, key)
                pos += 1
        else:
            if key == curses.KEY_ENTER:
                break
            if key == 27:
                curses.curs_set(0)
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    buf.pop(pos - 1)
                    pos -= 1
            elif key == curses.KEY_DC:
                if pos < len(buf):
                    buf.pop(pos)
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif key == curses.KEY_HOME:
                pos = 0
            elif key == curses.KEY_END:
                pos = len(buf)

    curses.curs_set(0)
    result = "".join(buf).strip()
    return result if result else None


def _choose(stdscr, row: int, prompt: str, options: list[tuple[str, str]]) -> "str | None":
    """Key-choice menu on *row*. Returns chosen key (lowercase) or None on Esc."""
    _, w = stdscr.getmaxyx()
    parts = "  ".join(f"[{k.upper()}] {lbl}" for k, lbl in options)
    line  = f" {prompt}:  {parts}  [Esc] Cancel"
    _put(stdscr, row, 0, line[:w - 1].ljust(w - 1),
         curses.color_pair(C_HDR) | curses.A_BOLD)
    stdscr.refresh()
    while True:
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        ch = key if isinstance(key, str) else (chr(key) if 0 < key < 256 else "")
        if ch == "\x1b" or key == 27:
            return None
        ch = ch.lower()
        for k, _ in options:
            if ch == k.lower():
                return ch
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()


# ── Pending-edit model ────────────────────────────────────────────────────────

class PendingEdit:
    def __init__(self, desc: str):
        self.desc            = desc
        self.tag_writes:     list[tuple[Path, dict[str, str]]] = []
        self.file_renames:   list[tuple[Path, Path]]           = []
        self.dir_renames:    list[tuple[Path, Path]]           = []
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

    edit = PendingEdit(f"Artist rename: {artist.label!r} → {new_name!r}")
    edit.preview_labels[id(artist)] = new_name

    for album in artist.children:
        w = _track_width(album)
        for track in album.children:
            t = track.tags
            if not t:
                continue
            num = _track_num(track)
            if num is None:
                continue
            title_s  = _sanitize(t.get("title", ""))
            new_name_ = _new_track_filename(num, w, new_name, title_s)
            new_path  = track.path.parent / new_name_
            edit.tag_writes.append((track.path, {"TPE1": new_tag}))
            if new_path != track.path:
                edit.file_renames.append((track.path, new_path))

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

    edit = PendingEdit(f"Album title: {album.label!r} → {folder!r}")
    edit.preview_labels[id(album)] = folder
    for track in album.children:
        if track.tags:
            edit.tag_writes.append((track.path, {"TALB": new_title}))
    if new_dir != album.path:
        edit.dir_renames.append((album.path, new_dir))
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
    """Move album to a different artist folder, retag TPE1, rename files."""
    new_artist = _sanitize(raw)
    new_tag    = _normalize(raw)
    if not new_artist:
        return None
    load_album_tags(album)
    w = _track_width(album)

    # New location: sibling of current artist folder, same album folder name
    new_artist_dir = album.path.parent.parent / new_artist
    new_album_dir  = new_artist_dir / album.path.name

    edit = PendingEdit(f"Album artist → {new_artist!r}")
    edit.preview_labels[id(album)] = f"{album.label}  [→ {new_artist}]"

    for track in album.children:
        t = track.tags
        if not t:
            continue
        num = _track_num(track)
        if num is None:
            continue
        title_s  = _sanitize(t.get("title", ""))
        new_fname = _new_track_filename(num, w, new_artist, title_s)
        new_path  = track.path.parent / new_fname
        edit.tag_writes.append((track.path, {"TPE1": new_tag}))
        if new_path != track.path:
            edit.file_renames.append((track.path, new_path))

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

    return (not errors), "  |  ".join(errors)


# ── Edit dispatcher ───────────────────────────────────────────────────────────

def _do_edit(stdscr, node: Node) -> "PendingEdit | None":
    h, _ = stdscr.getmaxyx()
    bar   = h - 1

    if node.kind == TRACK:
        if not node.tags:
            load_album_tags(node.parent)
        cur = node.tags.get("title", node.path.stem)
        val = _text_input(stdscr, bar, f" Title [{cur}]: ", cur)
        return _build_track_title(node, val) if val else None

    elif node.kind == ALBUM:
        choice = _choose(stdscr, bar, "Edit album",
                         [("t", "Title"), ("y", "Year"), ("a", "Artist"), ("g", "Genre")])
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
            cur_a = next((tr.tags.get("artist", "") for tr in node.children if tr.tags.get("artist")), "")
            val = _text_input(stdscr, bar, f" Artist [{cur_a}]: ", cur_a)
            return _build_album_artist(node, val) if val else None

        elif choice == "g":
            cur_g = next((tr.tags.get("genre", "") for tr in node.children if tr.tags.get("genre")), "")
            val = _text_input(stdscr, bar, f" Genre [{cur_g}]: ", cur_g)
            return _build_album_genre(node, val) if val else None

    elif node.kind == ARTIST:
        choice = _choose(stdscr, bar, "Edit artist",
                         [("n", "Name"), ("g", "Genre")])
        if not choice:
            return None

        if choice == "n":
            val = _text_input(stdscr, bar, f" Artist name [{node.label}]: ", node.label)
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

        elif key == curses.KEY_RIGHT:
            node = items[sel]
            if node.kind != TRACK:
                if not node.expanded:
                    sel = _expand(node, artists, sel)
                elif sel + 1 < total and items[sel + 1].parent is node:
                    sel += 1

        elif key == curses.KEY_LEFT:
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
  python browse.py ~/Music               # library root (Artist/Album/mp3)
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


if __name__ == "__main__":
    main()
