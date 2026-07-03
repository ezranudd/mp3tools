"""
Pure-function tests for sync_library.py (no ffmpeg, no real devices).

Device detection itself (detect_devices) reads /proc/mounts and /sys/block, so
it is never called here — device_rows is exercised with detect_devices
monkeypatched out.
"""
from pathlib import Path

import sync_library
from sync_library import _is_syncable_mount, _partition_base, classify_device


def test_device_rows_excludes_library(tmp_path, monkeypatch):
    inside = tmp_path / "Artist"
    inside.mkdir()
    fake = [
        {"path": tmp_path, "free": 1, "total": 2},                 # the library itself
        {"path": inside, "free": 1, "total": 2},                   # inside the library
        {"path": Path("/run/media/x/USB"), "free": 100, "total": 200},
    ]
    monkeypatch.setattr(sync_library, "detect_devices", lambda: fake)
    rows = sync_library.device_rows(exclude=tmp_path)
    paths = [r["path"] for r in rows]
    assert str(tmp_path) not in paths
    assert str(inside) not in paths
    assert "/run/media/x/USB" in paths
    assert rows[0]["name"] == "USB" and rows[0]["free_h"]
    assert rows[0]["type"] == "generic"   # no "type" in the fake → defaulted


def test_device_rows_passes_type(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_library, "detect_devices",
                        lambda: [{"path": Path("/run/media/x/SD"), "free": 1,
                                  "total": 2, "type": "sd"}])
    rows = sync_library.device_rows()
    assert rows[0]["type"] == "sd"


def test_partition_base():
    assert _partition_base("/dev/sdb1") == "sdb"
    assert _partition_base("/dev/mmcblk0p1") == "mmcblk0"
    assert _partition_base("/dev/nvme0n1p1") == "nvme0n1"


def test_classify_device(tmp_path):
    sysblock = tmp_path / "block"
    for name, removable in (("sdb", "1"), ("sda", "0")):
        (sysblock / name).mkdir(parents=True)
        (sysblock / name / "removable").write_text(removable + "\n")
    assert classify_device("/dev/mmcblk0p1", sysblock) == "sd"
    assert classify_device("/dev/sdb1", sysblock) == "usb"      # removable
    assert classify_device("/dev/sda1", sysblock) == "drive"    # fixed
    assert classify_device("/dev/nvme0n1p1", sysblock) == "drive"
    assert classify_device("/dev/mapper/luks-abc", sysblock) == "drive"   # LUKS/LVM
    assert classify_device(None, sysblock) == "generic"
    assert classify_device("//phone:mtp", sysblock) == "generic"


def test_is_syncable_mount():
    assert _is_syncable_mount("/media/u/CARD", "vfat")
    assert _is_syncable_mount("/mnt/usb", "ext4")
    assert not _is_syncable_mount("/", "ext4")
    assert not _is_syncable_mount("/boot/efi", "vfat")
    assert not _is_syncable_mount("/boot", "ext4")
    assert not _is_syncable_mount("/mnt/dvd", "iso9660")
    assert not _is_syncable_mount("/tmp", "tmpfs")
    assert not _is_syncable_mount("/sys/fs/cgroup", "cgroup2")
    # /run/media devices are excluded from the /proc/mounts loop (the /run prefix)
    # and instead enumerated via the _DEVICE_BASES scan.
    assert not _is_syncable_mount("/run/media/u/USB", "ext4")


# ── webjobs sync-progress display helpers ─────────────────────────────────────

def test_sync_eta_helper():
    from webjobs import _eta, _fmt_duration
    assert _fmt_duration(95) == "1:35"
    assert _fmt_duration(3725) == "1:02:05"
    assert _eta(0, 100, 10) == ""          # nothing copied yet
    assert _eta(100, 100, 10) == ""        # finished
    assert _eta(50, 100, 0.2) == ""        # too early to estimate
    assert _eta(50, 100, 1.0) == " · ETA 0:01"   # half done in 1s → ~1s left
