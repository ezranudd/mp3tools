#!/usr/bin/env python3
"""
Sync selected artist folders from a local MP3 library to a device.

The device receives the same Artist/Album/files layout as the library. For
selected artists, the sync mirrors the local artist folder: matching files are
skipped, missing or changed files are copied, and stale device files are removed.
"""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


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
    """Apply a SyncPlan (copy/remove files, prune empty dirs).

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
