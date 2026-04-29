#!/usr/bin/env python3
"""
Utilities for detecting and converting lossless audio (FLAC/ALAC) to MP3.
Used by both standardize.py (in-place conversion) and import_tracks.py (convert-on-copy).
"""

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

LOSSLESS_EXTENSIONS = {".flac", ".m4a", ".alac"}


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def is_alac(path: Path) -> bool:
    """Return True if the M4A file contains ALAC (lossless) audio, not AAC."""
    try:
        from mutagen.mp4 import MP4
        audio = MP4(str(path))
        return bool(audio.info and audio.info.codec and audio.info.codec.startswith("alac"))
    except Exception:
        return True  # assume lossless on read error (conservative)


def find_lossless(root: Path) -> list[Path]:
    """Return sorted list of all lossless files under root, skipping AAC .m4a files."""
    files = []
    for ext in LOSSLESS_EXTENSIONS:
        for path in root.rglob(f"*{ext}"):
            if ext == ".m4a" and not is_alac(path):
                print(f"  SKIP {path.name} (AAC inside .m4a container — not lossless)")
                continue
            files.append(path)
    return sorted(files)


def read_lossless_tags(path: Path) -> dict:
    """Read tags from a FLAC or M4A/ALAC file into the standard tag dict."""
    empty = {"TPE1": None, "ALBUMARTIST": None, "TIT2": None, "TALB": None,
             "YEAR": None, "TCON": None, "TRCK": None}

    def _year(raw: str | None) -> str | None:
        if not raw:
            return None
        m = re.search(r'\b(19\d{2}|20\d{2})\b', raw)
        return m.group(1) if m else raw[:4]

    if path.suffix.lower() == ".flac":
        try:
            from mutagen.flac import FLAC
            audio = FLAC(str(path))

            def g(key: str) -> str | None:
                v = audio.get(key)
                return v[0] if v else None

            trck_n = g("tracknumber")
            trck_t = g("totaltracks") or g("tracktotal")
            trck   = f"{trck_n}/{trck_t}" if trck_n and trck_t else trck_n

            disc_n = g("discnumber") or g("disc")
            disc_t = g("disctotal") or g("totaldiscs")
            tpos   = f"{disc_n}/{disc_t}" if disc_n and disc_t else disc_n

            return {
                "TPE1": g("artist"),
                "ALBUMARTIST": g("albumartist") or g("album artist") or g("album_artist"),
                "TIT2": g("title"),
                "TALB": g("album"),
                "YEAR": _year(g("date") or g("year")),
                "TCON": g("genre"),
                "TRCK": trck,
                "TPOS": tpos,
            }
        except Exception as e:
            print(f"  ERROR reading {path.name}: {e}")
            return empty

    else:  # .m4a / .alac
        try:
            from mutagen.mp4 import MP4
            audio = MP4(str(path))
            tags = audio.tags or {}

            def g(key: str) -> str | None:
                v = tags.get(key)
                if not v:
                    return None
                item = v[0]
                return str(item) if not isinstance(item, tuple) else None

            trkn = tags.get("trkn")
            trck = None
            if trkn and isinstance(trkn[0], tuple):
                num, total = trkn[0]
                trck = f"{num}/{total}" if total else str(num)

            disk = tags.get("disk")
            tpos = None
            if disk and isinstance(disk[0], tuple):
                dnum, dtotal = disk[0]
                tpos = f"{dnum}/{dtotal}" if dtotal else str(dnum)

            return {
                "TPE1": g("\xa9ART"),
                "ALBUMARTIST": g("aART"),
                "TIT2": g("\xa9nam"),
                "TALB": g("\xa9alb"),
                "YEAR": _year(g("\xa9day")),
                "TCON": g("\xa9gen"),
                "TRCK": trck,
                "TPOS": tpos,
            }
        except Exception as e:
            print(f"  ERROR reading {path.name}: {e}")
            return empty


def prompt_bitrate() -> int | None:
    """Ask user to pick 192/256/320 kbps or skip. Returns kbps int or None."""
    while True:
        try:
            choice = input(
                "  Convert to: [1] 192 kbps  [2] 256 kbps  [3] 320 kbps  [S]kip: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice == "1":
            return 192
        if choice == "2":
            return 256
        if choice == "3":
            return 320
        if choice in ("s", ""):
            return None


def convert_to_mp3(src: Path, dst: Path, bitrate: int,
                   start_time: float | None = None,
                   end_time: float | None = None) -> bool:
    """Convert src lossless file to MP3 at bitrate kbps using ffmpeg. Returns True on success."""
    try:
        cmd = ["ffmpeg", "-i", str(src)]
        if start_time is not None:
            cmd += ["-ss", f"{start_time:.6f}"]
        if end_time is not None:
            cmd += ["-to", f"{end_time:.6f}"]
        cmd += ["-acodec", "libmp3lame", "-b:a", f"{bitrate}k",
                "-map_metadata", "0", "-y", str(dst)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ffmpeg error: {result.stderr[-300:].strip()}")
            return False
        return True
    except FileNotFoundError:
        print("    ERROR: ffmpeg not found. Install it: sudo apt install ffmpeg")
        return False
    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def _cue_to_secs(ts: str) -> float:
    """Convert CUE MM:SS:FF timestamp to seconds (75 frames/sec)."""
    mm, ss, ff = (int(x) for x in ts.strip().split(":"))
    return mm * 60 + ss + ff / 75.0


def find_cue(flac_path: Path) -> Path | None:
    """Return the .cue file for flac_path (same stem, or any .cue in the same folder)."""
    same_stem = flac_path.with_suffix(".cue")
    if same_stem.exists():
        return same_stem
    cues = sorted(flac_path.parent.glob("*.cue"))
    return cues[0] if cues else None


def parse_cue(cue_path: Path) -> list[dict]:
    """Parse a .cue file, returning a list of track dicts with timing and tag info."""
    tracks = []
    album_artist = album_title = album_year = album_genre = None
    cur: dict | None = None

    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    for raw in text.splitlines():
        line  = raw.strip()
        upper = line.upper()

        if upper.startswith("TRACK "):
            if cur is not None:
                tracks.append(cur)
            try:
                num = int(line.split()[1])
            except (IndexError, ValueError):
                num = len(tracks) + 1
            cur = {"track_num": num, "title": None, "artist": None, "start_secs": None}

        elif cur is None:
            if upper.startswith("PERFORMER "):
                album_artist = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("TITLE "):
                album_title = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("REM DATE "):
                album_year = line.split(None, 2)[-1].strip()[:4]
            elif upper.startswith("REM GENRE "):
                album_genre = line.split(None, 2)[-1].strip().strip('"')

        else:
            if upper.startswith("TITLE "):
                cur["title"] = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("PERFORMER "):
                cur["artist"] = line.split(None, 1)[1].strip().strip('"')
            elif upper.startswith("INDEX 01 "):
                cur["start_secs"] = _cue_to_secs(line.split(None, 2)[2].strip())

    if cur is not None:
        tracks.append(cur)

    # Filter out malformed entries and compute end times
    tracks = [t for t in tracks if t["start_secs"] is not None]
    for i, t in enumerate(tracks):
        t["end_secs"] = tracks[i + 1]["start_secs"] if i + 1 < len(tracks) else None
        if t["artist"] is None:
            t["artist"] = album_artist
        t["album_artist"] = album_artist
        t["album_title"]  = album_title
        t["album_year"]   = album_year
        t["album_genre"]  = album_genre

    return tracks


def read_cue_tracks(flac_path: Path) -> list[tuple[Path, dict]] | None:
    """
    If flac_path has an associated .cue file, return one (flac_path, tagdict) per
    track. The tagdicts include _CUE_START/_CUE_END for timed conversion.
    Returns None if no usable .cue is found.
    """
    cue = find_cue(flac_path)
    if not cue:
        return None

    # A single-file CUE (whole album in one FLAC) has exactly one FILE directive.
    # Multi-file CUEs (one FLAC per track) have one FILE per track — skip those,
    # since each FLAC is already its own entry and the CUE is just a playlist.
    try:
        cue_text = cue.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    file_lines = [l for l in cue_text.splitlines()
                  if l.strip().upper().startswith("FILE ")]
    if len(file_lines) != 1:
        return None

    tracks = parse_cue(cue)
    if not tracks:
        return None

    total = len(tracks)
    result = []
    for t in tracks:
        td = {
            "TPE1": t["artist"],
            "ALBUMARTIST": t["album_artist"],
            "TIT2": t["title"] or f"Track {t['track_num']}",
            "TALB": t["album_title"],
            "YEAR": t["album_year"],
            "TCON": t["album_genre"],
            "TRCK": f"{t['track_num']}/{total}",
            "TPOS": None,
            "_CUE_START": t["start_secs"],
            "_CUE_END":   t["end_secs"],
        }
        result.append((flac_path, td))
    return result


def step_convert_lossless(root: Path, dry_run: bool) -> dict:
    """
    Step 0 for standardize: find FLAC/ALAC files in root, prompt for bitrate,
    convert each to MP3 in-place, and delete the original.
    """
    print(f"\n{'=' * 60}")
    print("Step 0: Convert lossless files (FLAC/ALAC)")
    print("=" * 60)

    lossless = find_lossless(root)
    if not lossless:
        print("  No lossless files found.")
        return {"converted": 0, "errors": 0}

    ext_counts = Counter(f.suffix.lower() for f in lossless)
    summary = ", ".join(
        f"{n} {ext.upper().lstrip('.')}" for ext, n in sorted(ext_counts.items())
    )
    print(f"  Found: {summary} ({len(lossless)} total)")
    for f in lossless:
        print(f"    {f.relative_to(root)}")
    print()

    if dry_run:
        print("  (dry run) Would prompt to convert at 192 / 256 / 320 kbps.")
        return {"converted": len(lossless), "errors": 0}

    if not _has_ffmpeg():
        print("  ERROR: ffmpeg not found. Install it: sudo apt install ffmpeg")
        print("  Skipping lossless conversion.")
        return {"converted": 0, "errors": len(lossless)}

    bitrate = prompt_bitrate()
    if bitrate is None:
        print("  Skipped.")
        return {"converted": 0, "errors": 0}

    stats = {"converted": 0, "errors": 0}
    for src in lossless:
        dst = src.with_suffix(".mp3")
        if dst.exists():
            print(f"  SKIP {src.name} — {dst.name} already exists")
            stats["errors"] += 1
            continue
        print(f"  {src.name}  ->  {dst.name}  [{bitrate} kbps]")
        if convert_to_mp3(src, dst, bitrate):
            src.unlink()
            stats["converted"] += 1
        else:
            stats["errors"] += 1

    print(f"\n  Converted: {stats['converted']}  Errors: {stats['errors']}")
    return stats
