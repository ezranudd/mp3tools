#!/usr/bin/env python3
"""
Sync selected artist folders from a local MP3 library to a device.

The device receives the same Artist/Album/files layout as the library. For
selected artists, the sync mirrors the local artist folder: matching files are
skipped, missing or changed files are copied, and stale device files are removed.
"""

import argparse
import curses
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from termtext import cell_width, clip_cells, fit_cells


C_HDR = 1
C_BAR = 2
C_SEL = 3
C_DIM = 4
C_OK  = 5
C_WARN = 6


@dataclass
class ArtistInfo:
    path: Path
    size: int
    files: int
    device_status: str
    selected: bool = False


@dataclass
class SyncPlan:
    copy_files: list[tuple[Path, Path]]
    remove_files: list[Path]
    remove_dirs: list[Path]
    bytes_to_copy: int
    bytes_to_remove: int


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def iter_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))


def folder_size(root: Path) -> tuple[int, int]:
    total = 0
    count = 0
    for p in iter_files(root):
        try:
            total += p.stat().st_size
            count += 1
        except OSError:
            pass
    return total, count


def file_matches(src: Path, dst: Path) -> bool:
    try:
        s1 = src.stat()
        s2 = dst.stat()
    except OSError:
        return False
    return s1.st_size == s2.st_size and int(s1.st_mtime) == int(s2.st_mtime)


def artist_dirs(library: Path) -> list[Path]:
    seen: set[Path] = set()
    for mp3 in library.rglob("*.mp3"):
        rel = mp3.relative_to(library)
        if rel.parts:
            candidate = library / rel.parts[0]
            if candidate.is_dir() and not candidate.name.startswith("."):
                seen.add(candidate)
    return sorted(seen)


def synced_albums(device_artist: Path) -> list[str]:
    if not device_artist.is_dir():
        return []
    return sorted(
        d.name for d in device_artist.iterdir()
        if d.is_dir() and any(d.rglob("*.mp3"))
    )


def compare_artist(src_artist: Path, dst_artist: Path) -> str:
    if not dst_artist.exists():
        return "not on device"

    src_files = {p.relative_to(src_artist): p for p in iter_files(src_artist)}
    dst_files = {p.relative_to(dst_artist): p for p in iter_files(dst_artist)}

    missing = [rel for rel in src_files if rel not in dst_files]
    extra = [rel for rel in dst_files if rel not in src_files]
    changed = [
        rel for rel in src_files
        if rel in dst_files and not file_matches(src_files[rel], dst_files[rel])
    ]

    if not missing and not extra and not changed:
        return "synced"

    parts = []
    if missing:
        parts.append(f"{len(missing)} missing")
    if changed:
        parts.append(f"{len(changed)} changed")
    if extra:
        parts.append(f"{len(extra)} extra")
    return ", ".join(parts)


def build_artist_info(library: Path, device: Path) -> list[ArtistInfo]:
    infos = []
    for artist in artist_dirs(library):
        size, files = folder_size(artist)
        infos.append(ArtistInfo(
            path=artist,
            size=size,
            files=files,
            device_status=compare_artist(artist, device / artist.name),
        ))
    return infos


def selected_size(artists: list[ArtistInfo]) -> int:
    return sum(a.size for a in artists if a.selected)


def existing_device_artists(device: Path) -> list[tuple[str, list[str]]]:
    if not device.is_dir():
        return []
    rows = []
    for artist in sorted(d for d in device.iterdir() if d.is_dir() and not d.name.startswith(".")):
        albums = synced_albums(artist)
        if albums or any(artist.rglob("*.mp3")):
            rows.append((artist.name, albums))
    return rows


def make_plan(src_artist: Path, dst_artist: Path) -> SyncPlan:
    src_files = {p.relative_to(src_artist): p for p in iter_files(src_artist)}
    dst_files = {p.relative_to(dst_artist): p for p in iter_files(dst_artist)} if dst_artist.exists() else {}

    copy_files: list[tuple[Path, Path]] = []
    bytes_to_copy = 0
    for rel, src in src_files.items():
        dst = dst_artist / rel
        if rel not in dst_files or not file_matches(src, dst):
            copy_files.append((src, dst))
            try:
                bytes_to_copy += src.stat().st_size
            except OSError:
                pass

    remove_files = [dst_files[rel] for rel in dst_files if rel not in src_files]
    bytes_to_remove = 0
    for path in remove_files:
        try:
            bytes_to_remove += path.stat().st_size
        except OSError:
            pass

    remove_dirs: list[Path] = []
    if dst_artist.exists():
        src_dirs = {p.relative_to(src_artist) for p in src_artist.rglob("*") if p.is_dir()}
        dst_dirs = {p.relative_to(dst_artist) for p in dst_artist.rglob("*") if p.is_dir()}
        for rel in sorted(dst_dirs - src_dirs, key=lambda p: len(p.parts), reverse=True):
            remove_dirs.append(dst_artist / rel)

    return SyncPlan(copy_files, remove_files, remove_dirs, bytes_to_copy, bytes_to_remove)


def combined_plan(library: Path, device: Path, artists: list[ArtistInfo]) -> SyncPlan:
    all_copy: list[tuple[Path, Path]] = []
    all_remove_files: list[Path] = []
    all_remove_dirs: list[Path] = []
    copy_bytes = 0
    remove_bytes = 0

    for artist in artists:
        if not artist.selected:
            continue
        plan = make_plan(artist.path, device / artist.path.name)
        all_copy.extend(plan.copy_files)
        all_remove_files.extend(plan.remove_files)
        all_remove_dirs.extend(plan.remove_dirs)
        copy_bytes += plan.bytes_to_copy
        remove_bytes += plan.bytes_to_remove

    return SyncPlan(all_copy, all_remove_files, all_remove_dirs, copy_bytes, remove_bytes)


def _init_colors() -> None:
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_HDR, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_BAR, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    except curses.error:
        pass


def _put(win, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        h, w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            win.addstr(y, x, clip_cells(text, max(0, w - x - 1)), attr)
    except curses.error:
        pass


def _bar(done: int, total: int, width: int) -> str:
    width = max(8, width)
    if total <= 0:
        filled = width
    else:
        filled = min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _existing_lines(existing: list[tuple[str, list[str]]], limit: int) -> list[str]:
    lines: list[str] = []
    for name, albums in existing:
        detail = f"{len(albums)} album{'s' if len(albums) != 1 else ''}" if albums else "music"
        lines.append(f"{name} ({detail})")
        for album in albums[:3]:
            lines.append(f"  {album}")
        if len(albums) > 3:
            lines.append(f"  ... {len(albums) - 3} more")
        if len(lines) >= limit:
            break
    return lines[:limit]


def draw_artist_menu(
    stdscr,
    library: Path,
    device: Path,
    artists: list[ArtistInfo],
    existing: list[tuple[str, list[str]]],
    dry_run: bool,
    sel: int,
    scroll: int,
    flash: str = "",
) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    usage = shutil.disk_usage(device)
    selected_count = sum(1 for a in artists if a.selected)

    mode = "DRY RUN" if dry_run else "LIVE"
    header = f" SYNC  {mode}  Library: {library}  Device: {device} "
    _put(stdscr, 0, 0, fit_cells(header, w - 1), curses.color_pair(C_HDR) | curses.A_BOLD)

    summary = (
        f"Free {format_size(usage.free)} / {format_size(usage.total)}   "
        f"Selected {selected_count}/{len(artists)} artists, {format_size(selected_size(artists))}"
    )
    _put(stdscr, 1, 1, summary, curses.A_BOLD)

    split = max(48, int(w * 0.66))
    right_x = min(w - 24, split + 2)
    list_w = max(20, right_x - 2)
    list_h = max(4, h - 5)

    _put(stdscr, 3, 1, "Artists", curses.A_BOLD)
    _put(stdscr, 3, right_x, "Already on device", curses.A_BOLD)

    visible = artists[scroll:scroll + list_h]
    for i, artist in enumerate(visible):
        idx = scroll + i
        y = 4 + i
        mark = "x" if artist.selected else " "
        name_w = max(12, list_w - 55)
        status_w = max(0, list_w - 33 - name_w)
        prefix = f"[{mark}] {idx + 1:>3}. "
        name_col = fit_cells(artist.path.name, name_w)
        status_col = clip_cells(artist.device_status, status_w)
        row = (
            f"{prefix}{name_col} "
            f"{format_size(artist.size):>9} {artist.files:>5} files  {status_col}"
        )
        attr = curses.color_pair(C_SEL) if idx == sel else 0
        _put(stdscr, y, 1, fit_cells(row, list_w), attr)

    right_h = max(0, h - 6)
    for i, line in enumerate(_existing_lines(existing, right_h)):
        _put(stdscr, 4 + i, right_x, clip_cells(line, max(10, w - right_x - 1)), curses.color_pair(C_DIM))

    footer = " ↑↓/j/k Move  Space Toggle  a All  n None  s Sync  q Cancel "
    if flash:
        footer = " " + flash
    _put(stdscr, h - 1, 0, fit_cells(footer, w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()


def draw_plan(stdscr, plan: SyncPlan, selected_count: int, free_space: int, dry_run: bool, message: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    net_needed = max(0, plan.bytes_to_copy - plan.bytes_to_remove)
    _put(stdscr, 0, 0, fit_cells(" SYNC PLAN ", w - 1), curses.color_pair(C_HDR) | curses.A_BOLD)
    rows = [
        f"Artists selected : {selected_count}",
        f"Files to copy    : {len(plan.copy_files)} ({format_size(plan.bytes_to_copy)})",
        f"Files to delete  : {len(plan.remove_files)} ({format_size(plan.bytes_to_remove)})",
        f"Free space       : {format_size(free_space)}",
        f"Net needed       : {format_size(net_needed)}",
    ]
    for i, row in enumerate(rows, 2):
        _put(stdscr, i, 2, row)

    if message:
        _put(stdscr, 8, 2, message, curses.color_pair(C_WARN) | curses.A_BOLD)

    key_line = " Enter Apply dry-run preview " if dry_run else " Type YES then Enter to apply, Esc to cancel "
    _put(stdscr, h - 1, 0, fit_cells(key_line, w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()


def confirm_live(stdscr, plan: SyncPlan, selected_count: int, free_space: int, dry_run: bool) -> bool:
    if dry_run:
        draw_plan(stdscr, plan, selected_count, free_space, dry_run)
        stdscr.getch()
        return True

    buf: list[str] = []
    while True:
        draw_plan(stdscr, plan, selected_count, free_space, dry_run, "Confirm: " + "".join(buf))
        key = stdscr.get_wch()
        if key == "\x1b" or key == 27:
            return False
        if key in ("\n", "\r") or key == curses.KEY_ENTER:
            return "".join(buf) == "YES"
        if key in ("\x7f", "\b") or key == curses.KEY_BACKSPACE:
            if buf:
                buf.pop()
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            buf.append(key)


def draw_progress(
    stdscr,
    action: str,
    current: str,
    done_files: int,
    total_files: int,
    done_bytes: int,
    total_bytes: int,
    dry_run: bool,
) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    title = " SYNC PREVIEW " if dry_run else " SYNC IN PROGRESS "
    _put(stdscr, 0, 0, fit_cells(title, w - 1), curses.color_pair(C_HDR) | curses.A_BOLD)
    _put(stdscr, 2, 2, f"Action : {action}")
    _put(stdscr, 3, 2, f"File   : {clip_cells(current, max(10, w - 11), chr(0x2026))}")
    _put(stdscr, 5, 2, f"Files  : {done_files}/{total_files}")
    _put(stdscr, 6, 2, _bar(done_files, total_files, max(10, w - 6)))
    _put(stdscr, 8, 2, f"Bytes  : {format_size(done_bytes)} / {format_size(total_bytes)}")
    _put(stdscr, 9, 2, _bar(done_bytes, total_bytes, max(10, w - 6)))
    _put(stdscr, h - 1, 0, fit_cells(" Working... ", w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()


def copy_with_progress(src: Path, dst: Path, progress) -> int:
    copied = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
                copied += len(chunk)
                progress(len(chunk))
        shutil.copystat(src, tmp)
        tmp.rename(dst)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return copied


def apply_plan(stdscr, plan: SyncPlan, dry_run: bool) -> tuple[int, int, int]:
    copied = 0
    removed_files = 0
    removed_dirs = 0
    total_files = len(plan.remove_files) + len(plan.remove_dirs) + len(plan.copy_files)
    total_bytes = plan.bytes_to_remove + plan.bytes_to_copy
    done_files = 0
    done_bytes = 0

    for path in plan.remove_files:
        draw_progress(stdscr, "Delete", path.name, done_files, total_files, done_bytes, total_bytes, dry_run)
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass
        if not dry_run:
            path.unlink()
        done_files += 1
        done_bytes += size
        draw_progress(stdscr, "Delete", path.name, done_files, total_files, done_bytes, total_bytes, dry_run)
        removed_files += 1

    for path in plan.remove_dirs:
        draw_progress(stdscr, "Remove folder", path.name, done_files, total_files, done_bytes, total_bytes, dry_run)
        if not dry_run and path.exists():
            try:
                path.rmdir()
                removed_dirs += 1
            except OSError:
                pass
        elif dry_run:
            removed_dirs += 1
        done_files += 1
        draw_progress(stdscr, "Remove folder", path.name, done_files, total_files, done_bytes, total_bytes, dry_run)

    for src, dst in plan.copy_files:
        label = src.name
        draw_progress(stdscr, "Copy", label, done_files, total_files, done_bytes, total_bytes, dry_run)
        if dry_run:
            try:
                done_bytes += src.stat().st_size
            except OSError:
                pass
        else:
            def progress(delta: int) -> None:
                nonlocal done_bytes
                done_bytes += delta
                draw_progress(stdscr, "Copy", label, done_files, total_files, done_bytes, total_bytes, dry_run)
            copy_with_progress(src, dst, progress)
        done_files += 1
        draw_progress(stdscr, "Copy", label, done_files, total_files, done_bytes, total_bytes, dry_run)
        copied += 1

    return copied, removed_files, removed_dirs


def draw_result(stdscr, copied: int, removed_files: int, removed_dirs: int, dry_run: bool) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    _put(stdscr, 0, 0, fit_cells(" SYNC COMPLETE ", w - 1), curses.color_pair(C_HDR) | curses.A_BOLD)
    _put(stdscr, 2, 2, f"Copied files    : {copied}")
    _put(stdscr, 3, 2, f"Deleted files   : {removed_files}")
    _put(stdscr, 4, 2, f"Removed folders : {removed_dirs}")
    if dry_run:
        _put(stdscr, 6, 2, "Dry run complete. Run in live mode to apply changes.", curses.color_pair(C_WARN))
    _put(stdscr, h - 1, 0, fit_cells(" Press any key to exit ", w - 1), curses.color_pair(C_BAR))
    stdscr.refresh()
    stdscr.getch()


def _run_curses(stdscr, library: Path, device: Path, dry_run: bool, artists: list[ArtistInfo]) -> int:
    _init_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    existing = existing_device_artists(device)
    sel = 0
    scroll = 0
    flash = ""

    while True:
        h, _ = stdscr.getmaxyx()
        list_h = max(4, h - 5)
        sel = max(0, min(sel, len(artists) - 1))
        if sel < scroll:
            scroll = sel
        elif sel >= scroll + list_h:
            scroll = sel - list_h + 1
        scroll = max(0, scroll)

        draw_artist_menu(stdscr, library, device, artists, existing, dry_run, sel, scroll, flash)
        flash = ""
        key = stdscr.getch()

        if key in (ord("q"), ord("Q"), 27):
            return 0
        if key in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            sel = min(len(artists) - 1, sel + 1)
        elif key == curses.KEY_PPAGE:
            sel = max(0, sel - list_h)
        elif key == curses.KEY_NPAGE:
            sel = min(len(artists) - 1, sel + list_h)
        elif key in (ord(" "), 10, 13):
            artists[sel].selected = not artists[sel].selected
        elif key in (ord("a"), ord("A")):
            for artist in artists:
                artist.selected = True
        elif key in (ord("n"), ord("N")):
            for artist in artists:
                artist.selected = False
        elif key in (ord("s"), ord("S")):
            selected_count = sum(1 for a in artists if a.selected)
            if selected_count == 0:
                flash = "No artists selected."
                continue
            plan = combined_plan(library, device, artists)
            usage = shutil.disk_usage(device)
            net_needed = max(0, plan.bytes_to_copy - plan.bytes_to_remove)
            if net_needed > usage.free:
                flash = f"Not enough free space: need {format_size(net_needed)}, free {format_size(usage.free)}."
                continue
            if not confirm_live(stdscr, plan, selected_count, usage.free, dry_run):
                flash = "Sync cancelled."
                continue
            copied, removed_files, removed_dirs = apply_plan(stdscr, plan, dry_run)
            draw_result(stdscr, copied, removed_files, removed_dirs, dry_run)
            return 0
        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()


def run_sync(library: Path, device: Path, dry_run: bool) -> int:
    library = library.expanduser().resolve()
    device = device.expanduser().resolve()

    if not library.is_dir():
        print(f"ERROR: library is not a directory: {library}", file=sys.stderr)
        return 1
    if not device.is_dir():
        print(f"ERROR: device is not a directory: {device}", file=sys.stderr)
        return 1
    if library == device or library in device.parents:
        print("ERROR: device cannot be the library directory or inside it", file=sys.stderr)
        return 1

    artists = build_artist_info(library, device)
    if not artists:
        print("No artist folders found in library.")
        return 0

    try:
        return curses.wrapper(_run_curses, library, device, dry_run, artists)
    except KeyboardInterrupt:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync selected library artists to a device")
    parser.add_argument("library", type=Path, help="Local MP3 library root")
    parser.add_argument("device", type=Path, help="Device mount/root directory")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying device")
    args = parser.parse_args()

    raise SystemExit(run_sync(args.library, args.device, args.dry_run))


if __name__ == "__main__":
    main()
