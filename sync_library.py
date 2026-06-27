#!/usr/bin/env python3
"""
Sync selected artist folders from a local MP3 library to a device.

The device receives the same Artist/Album/files layout as the library. For
selected artists, the sync mirrors the local artist folder: matching files are
skipped, missing or changed files are copied, and stale device files are removed.
"""

import argparse
import curses
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from termtext import clip_cells, fit_cells
from ui import (
    C_SEL, C_DIM, C_WARN,
    init_colors as _init_colors, header_bar, status_bar, keyhints, confirm_key,
    put as _put,
)


@dataclass
class AlbumInfo:
    path: Path
    size: int | None = None         # None until computed (lazy, local walk)
    files: int | None = None
    device_status: str | None = None  # None until computed (lazy, device walk)
    selected: bool = False


@dataclass
class ArtistInfo:
    path: Path
    size: int | None = None           # None until computed (lazy, local walk)
    files: int | None = None
    device_status: str | None = None  # None until computed (lazy, device walk)
    albums: list[AlbumInfo] | None = None  # None until enumerated
    expanded: bool = False
    # Used only for artists with no album subfolders (loose tracks): the whole
    # artist folder is the syncable unit.
    whole_selected: bool = False


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
    return s1.st_size == s2.st_size and abs(s1.st_mtime - s2.st_mtime) <= 2


def artist_dirs(library: Path) -> list[Path]:
    seen: set[Path] = set()
    for mp3 in library.rglob("*.mp3"):
        rel = mp3.relative_to(library)
        if rel.parts:
            candidate = library / rel.parts[0]
            if candidate.is_dir() and not candidate.name.startswith("."):
                seen.add(candidate)
    return sorted(seen)


def album_dirs(artist: Path) -> list[Path]:
    return sorted(
        d for d in artist.iterdir()
        if d.is_dir() and not d.name.startswith(".") and any(d.rglob("*.mp3"))
    )


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


def build_artist_info(library: Path) -> list[ArtistInfo]:
    """List artist folders only. Sizes and device status are computed lazily,
    so opening the sync screen stays fast even on a slow SD card."""
    return [ArtistInfo(path=artist) for artist in artist_dirs(library)]


def ensure_artist_size(artist: ArtistInfo) -> None:
    if artist.size is None:
        artist.size, artist.files = folder_size(artist.path)


def ensure_artist_status(artist: ArtistInfo, device: Path) -> None:
    if artist.device_status is None:
        artist.device_status = compare_artist(artist.path, device / artist.path.name)


def ensure_albums(artist: ArtistInfo) -> None:
    if artist.albums is None:
        artist.albums = [AlbumInfo(path=d) for d in album_dirs(artist.path)]


def ensure_album_size(album: AlbumInfo) -> None:
    if album.size is None:
        album.size, album.files = folder_size(album.path)


def ensure_album_status(album: AlbumInfo, artist: ArtistInfo, device: Path) -> None:
    if album.device_status is None:
        album.device_status = compare_artist(
            album.path, device / artist.path.name / album.path.name
        )


def artist_sel_state(artist: ArtistInfo) -> str:
    """'all', 'some', or 'none' selected — drives the [x]/[~]/[ ] marker."""
    if artist.albums is None:
        return "none"
    if not artist.albums:
        return "all" if artist.whole_selected else "none"
    sels = [a.selected for a in artist.albums]
    if all(sels):
        return "all"
    if any(sels):
        return "some"
    return "none"


def toggle_artist(artist: ArtistInfo) -> None:
    """Toggle every album of an artist on/off (select-all unless already all)."""
    ensure_albums(artist)
    if not artist.albums:
        artist.whole_selected = not artist.whole_selected
        return
    target = artist_sel_state(artist) != "all"
    for album in artist.albums:
        album.selected = target


def set_all_selected(artists: list[ArtistInfo], selected: bool) -> None:
    for artist in artists:
        ensure_albums(artist)
        if not artist.albums:
            artist.whole_selected = selected
        else:
            for album in artist.albums:
                album.selected = selected


def selection_summary(artists: list[ArtistInfo]) -> tuple[int, int]:
    """Return (artists with a selection, total albums selected)."""
    n_artists = 0
    n_albums = 0
    for artist in artists:
        if artist.albums is None:
            continue
        if not artist.albums:
            if artist.whole_selected:
                n_artists += 1
        else:
            sel = sum(1 for a in artist.albums if a.selected)
            if sel:
                n_artists += 1
                n_albums += sel
    return n_artists, n_albums


def selected_size(artists: list[ArtistInfo]) -> int:
    total = 0
    for artist in artists:
        if artist.albums is None:
            continue
        if not artist.albums:
            if artist.whole_selected:
                ensure_artist_size(artist)
                total += artist.size or 0
            continue
        sel = [a for a in artist.albums if a.selected]
        if sel and len(sel) == len(artist.albums):
            ensure_artist_size(artist)
            total += artist.size or 0
        else:
            for album in sel:
                ensure_album_size(album)
                total += album.size or 0
    return total


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

    def add(plan: SyncPlan) -> None:
        nonlocal copy_bytes, remove_bytes
        all_copy.extend(plan.copy_files)
        all_remove_files.extend(plan.remove_files)
        all_remove_dirs.extend(plan.remove_dirs)
        copy_bytes += plan.bytes_to_copy
        remove_bytes += plan.bytes_to_remove

    for artist in artists:
        if artist.albums is None:
            continue
        if not artist.albums:
            # Loose tracks directly under the artist folder: mirror the whole
            # artist folder when selected.
            if artist.whole_selected:
                add(make_plan(artist.path, device / artist.path.name))
            continue

        selected = [a for a in artist.albums if a.selected]
        if not selected:
            continue
        if len(selected) == len(artist.albums):
            # Whole artist selected: mirror the artist folder so albums removed
            # from the library are also pruned from the device.
            add(make_plan(artist.path, device / artist.path.name))
        else:
            # Partial selection: mirror only the chosen album folders. Unselected
            # albums already on the device are left untouched.
            for album in selected:
                add(make_plan(album.path, device / artist.path.name / album.path.name))

    return SyncPlan(all_copy, all_remove_files, all_remove_dirs, copy_bytes, remove_bytes)


# ── Web/headless helpers (no curses) ──────────────────────────────────────────

_SKIP_FS = {
    "sysfs", "proc", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "pstore", "bpf", "autofs", "mqueue", "hugetlbfs", "debugfs", "tracefs",
    "fusectl", "configfs", "securityfs", "efivarfs", "overlay", "nsfs",
    "ramfs", "squashfs",
    "iso9660", "udf",                       # optical discs (cdrom/dvd)
}
# /boot covers /boot and the EFI System Partition at /boot/efi.
_SKIP_PREFIXES = ("/sys", "/proc", "/dev", "/run", "/boot")
_DEVICE_BASES = (Path("/media"), Path("/mnt"), Path("/run/media"))


def _is_syncable_mount(mount: str, fstype: str) -> bool:
    """A real, user-facing volume — not system/virtual/optical/boot plumbing."""
    if fstype in _SKIP_FS or mount == "/":
        return False
    return not any(mount == p or mount.startswith(p + "/") for p in _SKIP_PREFIXES)


def _partition_base(node: str) -> str:
    """Block-device base of a partition node: /dev/sdb1->sdb, /dev/mmcblk0p1->mmcblk0,
    /dev/nvme0n1p1->nvme0n1."""
    name = node.rsplit("/", 1)[-1]
    if name.startswith(("mmcblk", "nvme")):
        return re.sub(r"p\d+$", "", name)
    return name.rstrip("0123456789")


def classify_device(source: str | None, sysblock: Path = Path("/sys/block")) -> str:
    """Best-effort device category from its source node: 'sd' | 'usb' | 'drive' |
    'generic'. (An SD card in a USB reader reads as a removable sd* → 'usb'.)"""
    if not source or not source.startswith("/dev/"):
        return "generic"
    if source.startswith(("/dev/mapper/", "/dev/dm-")):
        return "drive"          # LUKS/LVM volume — treat as a fixed drive
    base = _partition_base(source)
    if base.startswith("mmcblk"):
        return "sd"
    if base.startswith("nvme"):
        return "drive"
    if base.startswith("sd"):
        try:
            removable = (sysblock / base / "removable").read_text().strip()
        except OSError:
            removable = "0"
        return "usb" if removable == "1" else "drive"
    return "generic"


def _source_for(path: str, mounts_map: dict[str, str]) -> str | None:
    """Source device for a path: exact mountpoint, else the longest enclosing one."""
    if path in mounts_map:
        return mounts_map[path]
    best = None
    for mp, src in mounts_map.items():
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[0]):
                best = (mp, src)
    return best[1] if best else None


def detect_devices() -> list[dict]:
    """Detect mounted volumes to sync to: real filesystems plus the per-user
    mount points under /media, /mnt and /run/media. Returns
    [{"path": Path, "free": int|None, "total": int|None, "type": str}]."""
    seen: set[Path] = set()
    mounts_map: dict[str, str] = {}
    mount_fs: dict[str, str] = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                source, mount = parts[0], Path(parts[1])
                mounts_map[str(mount)] = source
                mount_fs[str(mount)] = parts[2]
                if (_is_syncable_mount(str(mount), parts[2])
                        and mount.is_dir() and mount not in seen):
                    seen.add(mount)
    except OSError:
        pass
    # The /media,/mnt,/run/media bases catch devices the loop above skipped via the
    # /run prefix — but only ones that are actually mounted (a real, non-optical fs),
    # so stale mountpoint dirs like /media/cdrom don't show up.
    for base in _DEVICE_BASES:
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            try:
                subs = [s for s in item.iterdir() if s.is_dir() and not s.name.startswith(".")]
            except OSError:
                subs = []
            for sub in (sorted(subs) if subs else [item]):
                fs = mount_fs.get(str(sub))
                if fs is not None and fs not in _SKIP_FS:
                    seen.add(sub)
    devices = []
    for path in sorted(seen):
        dev_type = classify_device(_source_for(str(path), mounts_map))
        try:
            usage = shutil.disk_usage(path)
            devices.append({"path": path, "free": usage.free,
                            "total": usage.total, "type": dev_type})
        except OSError:
            devices.append({"path": path, "free": None, "total": None, "type": dev_type})
    return devices


def device_rows(exclude: Path | None = None) -> list[dict]:
    """JSON-safe detected devices for the web sync view (skipping the library)."""
    rows = []
    for d in detect_devices():
        p = d["path"]
        if exclude and (p == exclude or exclude in p.parents):
            continue
        rows.append({
            "path": str(p), "name": p.name or str(p),
            "type": d.get("type", "generic"),
            "free": d["free"], "total": d["total"],
            "free_h": format_size(d["free"]) if d["free"] is not None else "?",
            "total_h": format_size(d["total"]) if d["total"] is not None else "?",
        })
    return rows


def artist_rows(library: Path, device: Path) -> list[dict]:
    """Artist-level rows for the web sync view (size + device status each)."""
    rows = []
    for ai in build_artist_info(library):
        ensure_artist_size(ai)
        ensure_artist_status(ai, device)
        rows.append({
            "path": str(ai.path), "name": ai.path.name,
            "size": ai.size or 0, "files": ai.files or 0,
            "size_h": format_size(ai.size or 0), "status": ai.device_status,
        })
    return rows


def album_rows(artist_path: Path, device: Path) -> dict:
    """Albums of one artist (loaded on expand), with size + device status."""
    ai = ArtistInfo(path=Path(artist_path))
    ensure_albums(ai)
    rows = []
    for al in (ai.albums or []):
        ensure_album_size(al)
        ensure_album_status(al, ai, device)
        rows.append({
            "path": str(al.path), "name": al.path.name,
            "size": al.size or 0, "files": al.files or 0,
            "size_h": format_size(al.size or 0), "status": al.device_status,
        })
    return {"has_albums": bool(ai.albums), "albums": rows}


def artists_from_selection(selection: dict) -> list[ArtistInfo]:
    """Reconstruct ArtistInfo objects (with .selected flags) from a web payload.

    selection maps an artist path to either the string "all" or a list of
    selected album paths. Mirrors how the TUI's ArtistInfo/AlbumInfo feed
    combined_plan().
    """
    artists: list[ArtistInfo] = []
    for apath, sel in selection.items():
        ai = ArtistInfo(path=Path(apath))
        ensure_albums(ai)
        if sel == "all":
            if ai.albums:
                for al in ai.albums:
                    al.selected = True
            else:
                ai.whole_selected = True
        elif isinstance(sel, (list, tuple)):
            wanted = {str(p) for p in sel}
            if ai.albums:
                for al in ai.albums:
                    if str(al.path) in wanted:
                        al.selected = True
            else:
                ai.whole_selected = bool(sel)
        artists.append(ai)
    return artists


def plan_summary(plan: SyncPlan, device: Path) -> dict:
    """JSON-safe summary of a SyncPlan plus device free-space figures."""
    usage = shutil.disk_usage(device)
    net = max(0, plan.bytes_to_copy - plan.bytes_to_remove)
    return {
        "copy_files": len(plan.copy_files),
        "remove_files": len(plan.remove_files),
        "remove_dirs": len(plan.remove_dirs),
        "bytes_to_copy": plan.bytes_to_copy,
        "bytes_to_remove": plan.bytes_to_remove,
        "free": usage.free, "total": usage.total, "net_needed": net,
        "enough_space": net <= usage.free,
        "copy_h": format_size(plan.bytes_to_copy),
        "remove_h": format_size(plan.bytes_to_remove),
        "free_h": format_size(usage.free), "net_h": format_size(net),
    }


def run_plan(plan: SyncPlan, dry_run: bool, *, on_progress=None) -> tuple[int, int, int]:
    """Apply a SyncPlan without curses (headless port of apply_plan).

    on_progress(action, name, done_files, total_files, done_bytes, total_bytes)
    is called as work proceeds. Returns (copied, removed_files, removed_dirs).
    """
    copied = removed_files = removed_dirs = 0
    total_files = len(plan.remove_files) + len(plan.remove_dirs) + len(plan.copy_files)
    total_bytes = plan.bytes_to_remove + plan.bytes_to_copy
    done_files = done_bytes = 0

    def emit(action: str, name: str) -> None:
        if on_progress:
            on_progress(action, name, done_files, total_files, done_bytes, total_bytes)

    for path in plan.remove_files:
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass
        done_files += 1
        done_bytes += size
        removed_files += 1
        emit("Delete", path.name)

    for path in plan.remove_dirs:
        if not dry_run and path.exists():
            try:
                path.rmdir()
                removed_dirs += 1
            except OSError:
                pass
        elif dry_run:
            removed_dirs += 1
        done_files += 1
        emit("Remove folder", path.name)

    for src, dst in plan.copy_files:
        if dry_run:
            try:
                done_bytes += src.stat().st_size
            except OSError:
                pass
        else:
            def progress(delta: int) -> None:
                nonlocal done_bytes
                done_bytes += delta
                emit("Copy", src.name)
            copy_with_progress(src, dst, progress)
        done_files += 1
        copied += 1
        emit("Copy", src.name)

    if not dry_run and (copied or removed_files):
        os.sync()

    return copied, removed_files, removed_dirs


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


def build_rows(artists: list[ArtistInfo]) -> list[tuple[int, int | None]]:
    """Flatten artists (and expanded albums) into navigable display rows.

    Each row is (artist_index, album_index) where album_index is None for an
    artist row and an int for an album row under an expanded artist.
    """
    rows: list[tuple[int, int | None]] = []
    for ai, artist in enumerate(artists):
        rows.append((ai, None))
        if artist.expanded and artist.albums:
            for bi in range(len(artist.albums)):
                rows.append((ai, bi))
    return rows


def _mark(state: str) -> str:
    return {"all": "x", "some": "~", "none": " "}.get(state, " ")


def draw_artist_menu(
    stdscr,
    library: Path,
    device: Path,
    artists: list[ArtistInfo],
    rows: list[tuple[int, int | None]],
    existing: list[tuple[str, list[str]]],
    dry_run: bool,
    sel: int,
    scroll: int,
    flash: str = "",
) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    usage = shutil.disk_usage(device)
    n_artists, n_albums = selection_summary(artists)

    mode = "Dry Run" if dry_run else "Live"
    header_bar(stdscr, f"Sync ({mode})", f"{library}  ->  {device}")

    summary = (
        f"Free {format_size(usage.free)} / {format_size(usage.total)}   "
        f"Selected {n_artists} artist{'s' if n_artists != 1 else ''}"
        f"/{n_albums} album{'s' if n_albums != 1 else ''}, "
        f"{format_size(selected_size(artists))}"
    )
    _put(stdscr, 1, 1, summary, curses.A_BOLD)

    split = max(48, int(w * 0.66))
    right_x = min(w - 24, split + 2)
    list_w = max(20, right_x - 2)
    list_h = max(4, h - 5)

    _put(stdscr, 3, 1, "Artists / albums", curses.A_BOLD)
    _put(stdscr, 3, right_x, "Already on device", curses.A_BOLD)

    name_w = max(12, list_w - 55)
    status_w = max(0, list_w - 35 - name_w)
    for i, (ai, bi) in enumerate(rows[scroll:scroll + list_h]):
        idx = scroll + i
        y = 4 + i
        artist = artists[ai]
        if bi is None:
            ensure_artist_size(artist)
            ensure_artist_status(artist, device)
            glyph = "v" if artist.expanded else ">"
            prefix = f"{glyph} [{_mark(artist_sel_state(artist))}] "
            name_col = fit_cells(artist.path.name, name_w)
            size, files, status = artist.size, artist.files, artist.device_status
        else:
            album = artist.albums[bi]
            ensure_album_size(album)
            ensure_album_status(album, artist, device)
            prefix = f"    [{'x' if album.selected else ' '}] "
            name_col = fit_cells(album.path.name, name_w)
            size, files, status = album.size, album.files, album.device_status
        row = (
            f"{prefix}{name_col} "
            f"{format_size(size or 0):>9} {files or 0:>5} files  "
            f"{clip_cells(status or '', status_w)}"
        )
        attr = curses.color_pair(C_SEL) if idx == sel else 0
        _put(stdscr, y, 1, fit_cells(row, list_w), attr)

    right_h = max(0, h - 6)
    for i, line in enumerate(_existing_lines(existing, right_h)):
        _put(stdscr, 4 + i, right_x, clip_cells(line, max(10, w - right_x - 1)), curses.color_pair(C_DIM))

    if flash:
        status_bar(stdscr, flash)
    else:
        status_bar(stdscr, keyhints([
            ("j/k", "Move"), ("g/G", "Top/Bottom"), ("Space", "Toggle"),
            ("→/←", "Expand/Collapse"), ("a", "All"), ("n", "None"),
            ("s", "Sync"), ("q", "Back"),
        ]))
    stdscr.refresh()


def draw_plan(stdscr, plan: SyncPlan, selected_count: int, free_space: int, footer: str = "") -> None:
    stdscr.erase()
    net_needed = max(0, plan.bytes_to_copy - plan.bytes_to_remove)
    header_bar(stdscr, "Sync Plan")
    rows = [
        f"Artists selected : {selected_count}",
        f"Files to copy    : {len(plan.copy_files)} ({format_size(plan.bytes_to_copy)})",
        f"Files to delete  : {len(plan.remove_files)} ({format_size(plan.bytes_to_remove)})",
        f"Free space       : {format_size(free_space)}",
        f"Net needed       : {format_size(net_needed)}",
    ]
    for i, row in enumerate(rows, 2):
        _put(stdscr, i, 2, row)

    if footer:
        status_bar(stdscr, footer)
    stdscr.refresh()


def confirm_live(stdscr, plan: SyncPlan, selected_count: int, free_space: int, dry_run: bool) -> bool:
    if dry_run:
        draw_plan(stdscr, plan, selected_count, free_space, "Press any key to run preview")
        stdscr.getch()
        return True

    draw_plan(stdscr, plan, selected_count, free_space)
    return confirm_key(stdscr, "Apply this sync to the device?")


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
    header_bar(stdscr, "Sync Preview" if dry_run else "Syncing")
    _put(stdscr, 2, 2, f"Action : {action}")
    _put(stdscr, 3, 2, f"File   : {clip_cells(current, max(10, w - 11), chr(0x2026))}")
    _put(stdscr, 5, 2, f"Files  : {done_files}/{total_files}")
    _put(stdscr, 6, 2, _bar(done_files, total_files, max(10, w - 6)))
    _put(stdscr, 8, 2, f"Bytes  : {format_size(done_bytes)} / {format_size(total_bytes)}")
    _put(stdscr, 9, 2, _bar(done_bytes, total_bytes, max(10, w - 6)))
    status_bar(stdscr, "Working...")
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

    if not dry_run and (copied or removed_files):
        # Flush OS write buffers to the card before reporting done, so the
        # device is safe to unmount the moment "Sync Complete" appears.
        draw_progress(stdscr, "Flushing buffers to device", "", total_files, total_files,
                      total_bytes, total_bytes, dry_run)
        os.sync()

    return copied, removed_files, removed_dirs


def draw_result(stdscr, copied: int, removed_files: int, removed_dirs: int, dry_run: bool) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    header_bar(stdscr, "Sync Complete")
    _put(stdscr, 2, 2, f"Copied files    : {copied}")
    _put(stdscr, 3, 2, f"Deleted files   : {removed_files}")
    _put(stdscr, 4, 2, f"Removed folders : {removed_dirs}")
    if dry_run:
        _put(stdscr, 6, 2, "Dry run complete. Run in live mode to apply changes.", curses.color_pair(C_WARN))
    status_bar(stdscr, "Press any key to continue")
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
        rows = build_rows(artists)
        sel = max(0, min(sel, len(rows) - 1))
        if sel < scroll:
            scroll = sel
        elif sel >= scroll + list_h:
            scroll = sel - list_h + 1
        scroll = max(0, scroll)

        draw_artist_menu(stdscr, library, device, artists, rows, existing, dry_run, sel, scroll, flash)
        flash = ""
        key = stdscr.getch()

        ai, bi = rows[sel]
        artist = artists[ai]

        if key in (ord("q"), ord("Q"), 27):
            return 0
        if key in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            sel = min(len(rows) - 1, sel + 1)
        elif key == curses.KEY_PPAGE:
            sel = max(0, sel - list_h)
        elif key == curses.KEY_NPAGE:
            sel = min(len(rows) - 1, sel + list_h)
        elif key in (ord("g"), curses.KEY_HOME):
            sel = 0
        elif key in (ord("G"), curses.KEY_END):
            sel = len(rows) - 1
        elif key in (curses.KEY_RIGHT, ord("l")):
            if bi is None:
                ensure_albums(artist)
                if artist.albums:
                    artist.expanded = True
        elif key in (curses.KEY_LEFT, ord("h")):
            if bi is None:
                artist.expanded = False
            else:
                # Collapse back to the parent artist row.
                artist.expanded = False
                sel = next((i for i, r in enumerate(build_rows(artists)) if r == (ai, None)), sel)
        elif key in (ord(" "), 10, 13):
            if bi is None:
                toggle_artist(artist)
            else:
                artist.albums[bi].selected = not artist.albums[bi].selected
        elif key in (ord("a"), ord("A")):
            set_all_selected(artists, True)
        elif key in (ord("n"), ord("N")):
            set_all_selected(artists, False)
        elif key in (ord("s"), ord("S")):
            n_artists, _ = selection_summary(artists)
            if n_artists == 0:
                flash = "Nothing selected."
                continue
            plan = combined_plan(library, device, artists)
            usage = shutil.disk_usage(device)
            net_needed = max(0, plan.bytes_to_copy - plan.bytes_to_remove)
            if net_needed > usage.free:
                flash = f"Not enough free space: need {format_size(net_needed)}, free {format_size(usage.free)}."
                continue
            if not confirm_live(stdscr, plan, n_artists, usage.free, dry_run):
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

    artists = build_artist_info(library)
    if not artists:
        print("No artist folders found in library.")
        return 0

    try:
        return curses.wrapper(_run_curses, library, device, dry_run, artists)
    except KeyboardInterrupt:
        return 0


def run_in_session(stdscr, library: Path, device: Path, dry_run: bool) -> None:
    """Enter sync view using an already-active curses session."""
    artists = build_artist_info(library)
    if not artists:
        return
    _init_colors()
    _run_curses(stdscr, library, device, dry_run, artists)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync selected library artists to a device")
    parser.add_argument("library", type=Path, help="Local MP3 library root")
    parser.add_argument("device", type=Path, help="Device mount/root directory")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying device")
    args = parser.parse_args()

    raise SystemExit(run_sync(args.library, args.device, args.dry_run))


if __name__ == "__main__":
    main()
