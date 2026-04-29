#!/usr/bin/env python3
"""
Curses import preview for import_tracks.py.

Shows a navigable artist/album/track tree built from in-memory tag dicts.
Lets the user edit any tag before import. Lossless bitrate is chosen here.

Returns (proceed: bool, lossless_bitrate: int | None).
Edits are applied to the entries list in-place.
"""

import curses
import os
import re
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("ESCDELAY", "25")

from browse import (
    Node, ARTIST, ALBUM, TRACK,
    visible,
    _init_colors, _put, _text_input, _choose,
    _normalize, _extract_year,
    C_ARTIST, C_ALBUM, C_TRACK, C_HDR, C_BAR, C_DIM,
)
from convert_lossless import LOSSLESS_EXTENSIONS

BITRATES       = [192, 256, 320]
_DEFAULT_RATE  = 320
C_FMT          = 8   # green — format badge [FLAC] etc.


# ── Key helpers ───────────────────────────────────────────────────────────────

def _artist_key(td: dict) -> str:
    return td.get("ALBUMARTIST") or td.get("TPE1") or "(Unknown Album Artist)"


def _album_key(td: dict) -> str:
    year  = td.get("YEAR") or ""
    album = td.get("TALB") or "(Unknown Album)"
    return f"{year} - {album}" if year else album


def _track_sort(src: Path, td: dict) -> tuple[int, int]:
    disc_raw = (td.get("TPOS") or "1").split("/")[0].strip()
    trck_raw = (td.get("TRCK") or "").split("/")[0].strip()
    try:
        disc = int(disc_raw)
    except ValueError:
        disc = 1
    try:
        track = int(trck_raw)
    except ValueError:
        m = re.match(r"^(\d+)", src.stem)
        track = int(m.group(1)) if m else 9999
    return (disc, track)


_DISC_RE = re.compile(
    r'[\s\-_]*([\(\[]?)(cd|disc|disk|part)\s*(\d+)([\)\]]?)\s*$',
    re.IGNORECASE,
)


def _disc_base(name: str) -> tuple[str, int]:
    """Strip a trailing disc indicator. Returns (base_name, disc_num)."""
    m = _DISC_RE.search(name)
    if m:
        return name[:m.start()].strip(), int(m.group(3))
    return name, 1


def _track_label(src: Path, td: dict, global_num: int | None = None,
                 num_width: int = 2) -> str:
    title = td.get("TIT2") or src.stem
    if global_num is not None:
        return f"{str(global_num).zfill(num_width)}. {title}"
    num = (td.get("TRCK") or "").split("/")[0].strip()
    if num.isdigit():
        return f"{num.zfill(num_width)}. {title}"
    return title


# ── Tree builder ──────────────────────────────────────────────────────────────

def _build_tree(entries: list[tuple[Path, dict]]) -> list[Node]:
    """Build a fully-expanded artist/album/track tree from in-memory entries."""
    # Preserve original discovery order so renamed albums append rather than interleave.
    entry_order = {id(td): i for i, (_, td) in enumerate(entries)}

    # Auto-merge multi-disc albums: "Album CD1" + "Album CD2" → "Album".
    # Group by (artist, base_name) and merge only when multiple disc numbers exist.
    disc_groups: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    for _, td in entries:
        album = td.get("TALB") or ""
        base, disc_num = _disc_base(album)
        if base != album:
            disc_groups[(_artist_key(td), base)].append((disc_num, td))

    for (_, base), disc_list in disc_groups.items():
        if len({disc for disc, _ in disc_list}) > 1:
            for disc_num, td in disc_list:
                td["TALB"] = base
                if not td.get("TPOS"):
                    td["TPOS"] = str(disc_num)

    groups: dict[str, dict[str, list[tuple[Path, dict]]]] = \
        defaultdict(lambda: defaultdict(list))
    for src, td in entries:
        groups[_artist_key(td)][_album_key(td)].append((src, td))

    artists: list[Node] = []
    for aname in sorted(groups, key=str.lower):
        anode = Node(ARTIST, aname, Path("."))
        anode.expanded = True
        for alkey in sorted(groups[aname]):
            alnode = Node(ALBUM, alkey, Path("."), parent=anode)
            alnode.expanded = True
            alnode.loaded   = True
            sorted_tracks = sorted(groups[aname][alkey],
                                   key=lambda x: entry_order[id(x[1])])
            num_width = 3 if len(sorted_tracks) >= 100 else 2
            for i, (src, td) in enumerate(sorted_tracks, 1):
                label   = _track_label(src, td, global_num=i, num_width=num_width)
                tnode   = Node(TRACK, label, src, parent=alnode)
                tnode.loaded = True
                tnode.tags   = {
                    "title":   td.get("TIT2") or "",
                    "artist":  td.get("TPE1") or "",
                    "albumartist": td.get("ALBUMARTIST") or "",
                    "album":   td.get("TALB") or "",
                    "year":    td.get("YEAR") or "",
                    "genre":   td.get("TCON") or "",
                    "track":   td.get("TRCK") or "",
                    "bitrate": str(td["_MP3_BITRATE"]) if td.get("_MP3_BITRATE") else "",
                }
                alnode.children.append(tnode)
            anode.children.append(alnode)
        artists.append(anode)
    return artists


# ── Position restore after rebuild ───────────────────────────────────────────

def _restore_sel(old_node: Node, new_items: list[Node], fallback: int) -> int:
    """Return the index in new_items that best matches old_node."""
    if old_node.kind == TRACK:
        for i, n in enumerate(new_items):
            if n.kind == TRACK and n.path == old_node.path:
                return i
    elif old_node.kind == ALBUM:
        old_paths = {c.path for c in old_node.children}
        for i, n in enumerate(new_items):
            if n.kind == ALBUM and any(c.path in old_paths for c in n.children):
                return i
    else:  # ARTIST
        for i, n in enumerate(new_items):
            if n.kind == ARTIST and n.label == old_node.label:
                return i
        old_paths = {c.path for alb in old_node.children for c in alb.children}
        for i, n in enumerate(new_items):
            if n.kind == ARTIST and any(
                c.path in old_paths for alb in n.children for c in alb.children
            ):
                return i
    return min(fallback, max(0, len(new_items) - 1))


# ── Entry lookup helpers ──────────────────────────────────────────────────────

def _album_entries(entries, artist_name, album_key):
    return [(s, t) for s, t in entries
            if _artist_key(t) == artist_name and _album_key(t) == album_key]


def _artist_entries(entries, artist_name):
    return [(s, t) for s, t in entries if _artist_key(t) == artist_name]


# ── Drawing ───────────────────────────────────────────────────────────────────

def _draw(stdscr, items: list[Node], sel: int, scroll: int,
          has_lossless: bool, lossless_bitrate: int | None,
          total_files: int, flash: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    list_h = max(1, h - 2)

    # ── Header ────────────────────────────────────────────────────────────────
    if has_lossless:
        br = f"{lossless_bitrate} kbps" if lossless_bitrate else "skip lossless"
        br_part = f"  Lossless:[b] {br}"
    else:
        br_part = ""
    hdr = (f" IMPORT PREVIEW  {total_files} file(s){br_part}"
           f"  ↑↓ j/k  →/← Expand  c/C Collapse  x Expand All  e Edit  p Proceed  q Abort ")
    _put(stdscr, 0, 0, hdr[:w - 1].ljust(w - 1),
         curses.color_pair(C_HDR) | curses.A_BOLD)

    if not items:
        _put(stdscr, 2, 2, "Nothing to import.", curses.A_DIM)
        stdscr.refresh()
        return

    # ── Tree rows ─────────────────────────────────────────────────────────────
    for i, node in enumerate(items[scroll : scroll + list_h]):
        row      = i + 1
        selected = (i + scroll) == sel
        fmt_tag  = ""

        if node.kind == ARTIST:
            arrow = "▼ " if node.expanded else "▶ "
            label = arrow + node.label
            na    = len(node.children)
            nt    = sum(len(a.children) for a in node.children)
            aside = (f"  {na:>3} album{'s' if na != 1 else ' '}"
                     f"  {nt:>4} track{'s' if nt != 1 else ' '}")
            base  = curses.color_pair(C_ARTIST) | curses.A_BOLD
        elif node.kind == ALBUM:
            arrow = "▼ " if node.expanded else "▶ "
            label = "  " + arrow + node.label
            nt    = len(node.children)
            aside = f"  {nt:>4} track{'s' if nt != 1 else ' '}"
            base  = curses.color_pair(C_ALBUM)
        else:
            ext       = node.path.suffix.lower()
            fmt_color = 0
            if ext in LOSSLESS_EXTENSIONS:
                ext_label = ext.upper().lstrip(".")
                br        = str(lossless_bitrate) if lossless_bitrate else "skip"
                fmt_tag   = f" [{ext_label}->{br}]"
                fmt_color = curses.color_pair(C_FMT) | curses.A_BOLD
            elif ext == ".mp3" and node.tags.get("bitrate"):
                fmt_tag   = f" [MP3 {node.tags['bitrate']}]"
                fmt_color = curses.color_pair(C_DIM)
            label = "      " + node.label
            aside = ""
            base  = curses.color_pair(C_TRACK)

        aside_w = len(aside)
        label_w = max(0, w - aside_w - 1)

        if fmt_tag:
            fmt_w  = len(fmt_tag)
            body_w = max(0, label_w - fmt_w)
            body_s = label[:body_w].ljust(body_w)
            if selected:
                _put(stdscr, row, 0,
                     (body_s + fmt_tag)[:w - 1].ljust(w - 1),
                     curses.A_REVERSE | curses.A_BOLD)
            else:
                _put(stdscr, row, 0, body_s, base)
                _put(stdscr, row, body_w, fmt_tag, fmt_color)
        else:
            label_s = label[:label_w].ljust(label_w)
            if selected:
                _put(stdscr, row, 0,
                     (label_s + aside)[:w - 1].ljust(w - 1),
                     curses.A_REVERSE | curses.A_BOLD)
            else:
                _put(stdscr, row, 0, label_s, base)
                if aside:
                    _put(stdscr, row, label_w, aside[:w - label_w - 1],
                         curses.color_pair(C_DIM) | curses.A_DIM)

    # ── Status bar ────────────────────────────────────────────────────────────
    if flash:
        info = " " + flash
    else:
        node = items[sel]
        if node.kind == TRACK:
            t   = node.tags
            ext = node.path.suffix.lower()
            parts = [t.get("title") or node.path.stem]
            for k in ("artist", "albumartist", "album", "year", "genre"):
                if t.get(k):
                    parts.append(t[k])
            if ext in LOSSLESS_EXTENSIONS:
                parts.append(ext.upper().lstrip("."))
            info = " " + "  │  ".join(parts)
        elif node.kind == ALBUM:
            nt   = len(node.children)
            par  = node.parent.label if node.parent else ""
            info = f" {node.label}  │  {par}  │  {nt} track{'s' if nt != 1 else ''}"
        else:
            na = len(node.children)
            nt = sum(len(a.children) for a in node.children)
            info = (f" {node.label}  │  {na} album{'s' if na != 1 else ''}"
                    f"  │  {nt} track{'s' if nt != 1 else ''}")

    _put(stdscr, h - 1, 0, info[:w - 1].ljust(w - 1),
         curses.color_pair(C_BAR))
    stdscr.refresh()


# ── Edit handler ──────────────────────────────────────────────────────────────

def _edit(stdscr, node: Node, entries: list[tuple[Path, dict]]) -> bool:
    """Edit tags for node. Modifies entries in-place. Returns True if changed."""
    h, _ = stdscr.getmaxyx()
    bar  = h - 1

    if node.kind == TRACK:
        choice = _choose(stdscr, bar, "Edit track",
                         [("t", "Title"), ("a", "Artist")])
        if not choice:
            return False

        key = "TIT2" if choice == "t" else "TPE1"
        label = "Title" if choice == "t" else "Artist"
        cur = (node.tags.get("title") if choice == "t" else node.tags.get("artist")) or node.path.stem
        val = _text_input(stdscr, bar, f" {label} [{cur}]: ", cur)
        if val:
            node_trck = node.tags.get("track", "").split("/")[0]
            for src, td in entries:
                if src != node.path:
                    continue
                if td.get("_CUE_START") is not None:
                    if td.get("TRCK", "").split("/")[0] != node_trck:
                        continue
                td[key] = _normalize(val)
                return True

    elif node.kind == ALBUM:
        aname  = node.parent.label if node.parent else ""
        alkey  = node.label
        aentries = _album_entries(entries, aname, alkey)
        if not aentries:
            return False

        choice = _choose(stdscr, bar, "Edit album",
                         [("t", "Title"), ("y", "Year"),
                          ("a", "Album Artist"), ("g", "Genre")])
        if not choice:
            return False

        first_td = aentries[0][1]

        if choice == "t":
            cur = first_td.get("TALB") or ""
            val = _text_input(stdscr, bar, f" Album title [{cur}]: ", cur)
            if val:
                v = _normalize(val)
                for _, td in aentries:
                    td["TALB"] = v
                return True

        elif choice == "y":
            cur = first_td.get("YEAR") or ""
            val = _text_input(stdscr, bar, f" Year [{cur}]: ", cur)
            if val:
                y = _extract_year(val) or val[:4]
                for _, td in aentries:
                    td["YEAR"] = y
                return True

        elif choice == "a":
            cur = first_td.get("ALBUMARTIST") or first_td.get("TPE1") or ""
            val = _text_input(stdscr, bar, f" Album artist [{cur}]: ", cur)
            if val:
                v = _normalize(val)
                for _, td in aentries:
                    td["ALBUMARTIST"] = v
                return True

        elif choice == "g":
            cur = first_td.get("TCON") or ""
            val = _text_input(stdscr, bar, f" Genre [{cur}]: ", cur)
            if val:
                v = _normalize(val)
                for _, td in aentries:
                    td["TCON"] = v
                return True

    elif node.kind == ARTIST:
        aname    = node.label
        aentries = _artist_entries(entries, aname)
        if not aentries:
            return False

        choice = _choose(stdscr, bar, "Edit artist",
                         [("n", "Album Artist"), ("g", "Genre")])
        if not choice:
            return False

        if choice == "n":
            val = _text_input(stdscr, bar, f" Album artist [{aname}]: ", aname)
            if val:
                v = _normalize(val)
                for _, td in aentries:
                    td["ALBUMARTIST"] = v
                return True

        elif choice == "g":
            cur = next((td.get("TCON", "") for _, td in aentries if td.get("TCON")), "")
            val = _text_input(stdscr, bar, f" Genre [{cur}]: ", cur)
            if val:
                v = _normalize(val)
                for _, td in aentries:
                    td["TCON"] = v
                return True

    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def _run(stdscr, entries: list[tuple[Path, dict]],
         has_lossless: bool) -> tuple[bool, int | None]:
    _init_colors()
    try:
        curses.init_pair(C_FMT, curses.COLOR_GREEN, -1)
    except curses.error:
        pass
    curses.curs_set(0)
    stdscr.keypad(True)

    lossless_bitrate: int | None = _DEFAULT_RATE if has_lossless else None
    artists  = _build_tree(entries)
    sel      = 0
    scroll   = 0
    flash    = ""
    total    = len(entries)

    while True:
        items  = visible(artists)
        n      = len(items)
        sel    = max(0, min(sel, n - 1))
        h, _   = stdscr.getmaxyx()
        list_h = max(1, h - 2)

        if sel < scroll:
            scroll = sel
        elif sel >= scroll + list_h:
            scroll = sel - list_h + 1
        scroll = max(0, scroll)

        _draw(stdscr, items, sel, scroll,
              has_lossless, lossless_bitrate, total, flash)
        flash = ""

        key = stdscr.getch()

        if key in (ord("q"), ord("Q"), 27):
            return False, None

        elif key in (ord("p"), ord("P")):
            return True, lossless_bitrate

        elif key == ord("b") and has_lossless:
            # cycle 192 → 256 → 320 → skip → 192 …
            if lossless_bitrate is None:
                lossless_bitrate = BITRATES[0]
            else:
                idx = BITRATES.index(lossless_bitrate)
                nxt = idx + 1
                lossless_bitrate = BITRATES[nxt] if nxt < len(BITRATES) else None

        elif key in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)

        elif key in (curses.KEY_DOWN, ord("j")):
            sel = min(n - 1, sel + 1)

        elif key == curses.KEY_PPAGE:
            sel = max(0, sel - list_h)

        elif key == curses.KEY_NPAGE:
            sel = min(n - 1, sel + list_h)

        elif key in (ord("g"), curses.KEY_HOME):
            sel = 0

        elif key in (ord("G"), curses.KEY_END):
            sel = n - 1

        elif key in (ord(" "), ord("\n"), 10, 13):
            node = items[sel]
            if node.kind != TRACK:
                if node.expanded:
                    node.expanded = False
                else:
                    node.expanded = True
                    if sel + 1 < len(visible(artists)):
                        sel += 1

        elif key == curses.KEY_RIGHT:
            node = items[sel]
            if node.kind != TRACK:
                if not node.expanded:
                    node.expanded = True
                    if sel + 1 < len(visible(artists)):
                        sel += 1
                elif sel + 1 < n and items[sel + 1].parent is node:
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

        elif key == ord("c"):
            for a in artists:
                for alb in a.children:
                    alb.expanded = False

        elif key == ord("C"):
            for a in artists:
                a.expanded = False

        elif key == ord("x"):
            for a in artists:
                a.expanded = True
                for alb in a.children:
                    alb.expanded = True

        elif key == ord("e"):
            old_node  = items[sel]
            changed   = _edit(stdscr, items[sel], entries)
            if changed:
                # Snapshot expand/collapse state before rebuild.
                known_a   = {a.label for a in artists}
                exp_a     = {a.label for a in artists if a.expanded}
                known_alb = {(a.label, alb.label)
                              for a in artists for alb in a.children}
                exp_alb   = {(a.label, alb.label)
                              for a in artists for alb in a.children
                              if alb.expanded}

                artists = _build_tree(entries)

                # Restore: known nodes keep their state; new nodes stay expanded.
                for a in artists:
                    if a.label in known_a:
                        a.expanded = a.label in exp_a
                    for alb in a.children:
                        key = (a.label, alb.label)
                        if key in known_alb:
                            alb.expanded = key in exp_alb

                sel   = _restore_sel(old_node, visible(artists), sel)
                flash = "Updated."

        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()


# ── Public API ────────────────────────────────────────────────────────────────

def run_preview(entries: list[tuple[Path, dict]],
                has_lossless: bool) -> tuple[bool, int | None]:
    """
    Show the import preview UI.

    Parameters
    ----------
    entries     : list of (source_path, tag_dict) — modified in-place by edits
    has_lossless: True if any lossless files are present in entries

    Returns
    -------
    (proceed, lossless_bitrate)
      proceed          — False means the user aborted
      lossless_bitrate — kbps int (192/256/320) or None (skip lossless)
    """
    try:
        return curses.wrapper(_run, entries, has_lossless)
    except KeyboardInterrupt:
        return False, None
