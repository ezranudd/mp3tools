#!/usr/bin/env python3
"""
Rip a CD to FLAC files with stub tags from MusicBrainz.
The resulting directory can be passed to import_tracks.import_tracks().

External requirements (all optional — gracefully degraded):
  cdparanoia   sudo apt install cdparanoia
  ffmpeg       sudo apt install ffmpeg
  python-discid (or cd-discid binary) for disc ID + track lengths
  musicbrainzngs                       for release metadata
"""

import re
import shutil
import subprocess
from pathlib import Path


# ── Device detection ──────────────────────────────────────────────────────────

_CD_DEVICE_PATHS = ["/dev/cdrom", "/dev/dvd", "/dev/sr0", "/dev/sr1", "/dev/sr2"]


def detect_cd_devices() -> list[Path]:
    """Return paths to optical drive block devices that exist on this system."""
    return [Path(d) for d in _CD_DEVICE_PATHS if Path(d).exists()]


# ── Disc TOC (ID + track lengths) ────────────────────────────────────────────

def read_disc_toc(device: str | Path) -> tuple[str | None, list[int]]:
    """
    Read the disc table of contents.
    Returns (disc_id, track_lengths_in_sectors).
    Either value may be empty/None if unavailable.
    """
    device = str(device)

    # discid gives us both the MB disc ID and per-track lengths in one shot
    try:
        import discid  # type: ignore
        disc = discid.read(device)
        lengths = [t.length for t in disc.tracks]
        return disc.id, lengths
    except ImportError:
        pass
    except Exception:
        pass

    # No discid — try to get just the disc ID from cd-discid
    disc_id: str | None = None
    try:
        result = subprocess.run(
            ["cd-discid", device],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            disc_id = result.stdout.strip().split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass

    # Query track lengths from cdparanoia -Q (TOC query, no ripping)
    track_lengths: list[int] = []
    try:
        result = subprocess.run(
            ["cdparanoia", "-Q", "-d", device],
            capture_output=True, text=True, timeout=30,
        )
        for line in (result.stdout + result.stderr).splitlines():
            # Matches: "  1.  17480 [03:53.05]  0 [00:02.00]  ..."
            m = re.match(r'^\s+\d+\.\s+(\d+)\s+\[', line)
            if m:
                track_lengths.append(int(m.group(1)))
    except Exception:
        pass

    return disc_id, track_lengths


# ── CD-Text ───────────────────────────────────────────────────────────────────

def read_cdtext(device: str | Path) -> dict | None:
    """
    Read CD-Text from the disc using cd-info (sudo apt install libcdio-utils).
    Returns {"artist", "album", "year", "genre", "tracks": [title, ...]}, or None
    if cd-info is not installed or the disc carries no CD-Text.
    """
    try:
        result = subprocess.run(
            ["cd-info", "--cdtext", "-q", str(device)],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
    except FileNotFoundError:
        return None
    except Exception:
        return None

    disc_title = disc_artist = ""
    track_titles: list[str] = []
    cur_title = cur_artist = ""
    in_disc = in_track = False

    def _flush() -> None:
        nonlocal cur_title, cur_artist
        track_titles.append(cur_title)
        cur_title = cur_artist = ""

    for raw in output.splitlines():
        line = raw.strip()

        if re.match(r"CD-Text for Disc", line, re.IGNORECASE):
            if in_track:
                _flush()
            in_disc, in_track = True, False
            continue

        if re.match(r"CD-Text for Track\s+\d+", line, re.IGNORECASE):
            if in_track:
                _flush()
            in_disc, in_track = False, True
            continue

        if not (in_disc or in_track):
            continue

        m = re.match(r"TITLE\s*[:\t]\s*(.*)", line, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if in_disc:
                disc_title = val
            else:
                cur_title = val
            continue

        m = re.match(r"PERFORMER\s*[:\t]\s*(.*)", line, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if in_disc:
                disc_artist = val
            else:
                cur_artist = val
            continue

    if in_track:
        _flush()

    if not disc_title and not disc_artist and not any(track_titles):
        return None

    return {
        "artist": disc_artist,
        "album":  disc_title,
        "year":   "",
        "genre":  "",
        "tracks": track_titles,
    }


# ── MusicBrainz lookup ────────────────────────────────────────────────────────

def lookup_musicbrainz(disc_id: str) -> dict | None:
    """
    Look up disc_id on MusicBrainz (requires: pip install musicbrainzngs).
    Returns {"artist", "album", "year", "genre", "tracks": [title, ...]}, or None.
    """
    try:
        import musicbrainzngs  # type: ignore
        musicbrainzngs.set_useragent("mp3tools", "1.0", "")
        result = musicbrainzngs.get_releases_by_discid(
            disc_id, includes=["artists", "recordings"],
        )
    except ImportError:
        return None
    except Exception:
        return None

    releases = result.get("disc", {}).get("release-list", [])
    if not releases:
        return None

    rel = releases[0]
    credits = rel.get("artist-credit", [])
    artist = (credits[0].get("artist", {}).get("name", "")
              if credits and isinstance(credits[0], dict) else "")
    album = rel.get("title", "")
    m = re.search(r"\b(19\d{2}|20\d{2})\b", rel.get("date", ""))
    year = m.group(1) if m else ""

    tracks: list[str] = []
    for medium in rel.get("medium-list", []):
        for track in medium.get("track-list", []):
            tracks.append(track.get("recording", {}).get("title", ""))

    return {"artist": artist, "album": album, "year": year, "genre": "", "tracks": tracks}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _log(log_fn, msg: str) -> None:
    if log_fn:
        log_fn(msg)
    else:
        print(msg)


def _write_flac_tags(flac: Path, track_num: int, total: int, info: dict) -> None:
    """Write Vorbis comment tags to a FLAC file using mutagen."""
    try:
        from mutagen.flac import FLAC
        audio = FLAC(str(flac))
        audio["tracknumber"] = str(track_num)
        audio["totaltracks"] = str(total)
        if info.get("artist"):
            audio["artist"]      = info["artist"]
            audio["albumartist"] = info["artist"]
        if info.get("album"):
            audio["album"] = info["album"]
        if info.get("year"):
            audio["date"] = info["year"]
        tracks = info.get("tracks", [])
        if 0 < track_num <= len(tracks) and tracks[track_num - 1]:
            audio["title"] = tracks[track_num - 1]
        audio.save()
    except Exception as e:
        _log(None, f"  WARNING: could not write tags to {flac.name}: {e}")


def _wav_to_flac(wav: Path, flac: Path, log_fn=None) -> bool:
    """Losslessly convert a WAV file to FLAC using ffmpeg. Returns True on success."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-i", str(wav), "-c:a", "flac", "-y", str(flac)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _log(log_fn, f"  ffmpeg error: {result.stderr[-200:].strip()}")
            return False
        return True
    except FileNotFoundError:
        _log(log_fn, "  ERROR: ffmpeg not found — install: sudo apt install ffmpeg")
        return False


# ── Regex for cdparanoia progress lines ───────────────────────────────────────

# Matches sector-progress lines: "## 1234 [wrote-to-output] @ 0xABC"
_SECTOR_RE = re.compile(r"##:?\s+(\d+)")
# Matches track-start lines in batch mode output
_TRACK_RE  = re.compile(r"track\s*(\d+)", re.IGNORECASE)


# ── Main entry point ──────────────────────────────────────────────────────────

def rip(device: str | Path, dest_dir: Path, *,
        log_fn=None, progress_fn=None, cancel_event=None) -> bool:
    """
    Rip the CD in `device` to FLAC files in `dest_dir`, writing stub Vorbis
    tags from MusicBrainz when a match is found.

    log_fn(str)                   – called for each status/log line
    progress_fn(track, total, pct)
      pct 0–100  : sector-level ripping progress for this track
      pct -1     : WAV→FLAC conversion phase (track/total = files done/total)
    cancel_event : threading.Event — set by caller to abort; subprocess is killed

    Returns True if at least one FLAC was produced.
    """
    device = str(device)

    if not shutil.which("cdparanoia"):
        _log(log_fn, "ERROR: cdparanoia not installed.")
        _log(log_fn, "  Install: sudo apt install cdparanoia")
        return False

    if not shutil.which("ffmpeg"):
        _log(log_fn, "ERROR: ffmpeg not installed.")
        _log(log_fn, "  Install: sudo apt install ffmpeg")
        return False

    # ── Disc TOC, MusicBrainz, and CD-Text metadata ───────────────────────────
    _log(log_fn, "Reading disc...")
    disc_id, track_lengths = read_disc_toc(device)
    meta: dict | None = None

    if disc_id:
        _log(log_fn, f"Disc ID: {disc_id}")
        _log(log_fn, "Looking up MusicBrainz...")
        meta = lookup_musicbrainz(disc_id)
        if meta:
            suffix = f" ({meta['year']})" if meta.get("year") else ""
            _log(log_fn, f"MusicBrainz: {meta['artist']} – {meta['album']}{suffix}")
        else:
            _log(log_fn, "No MusicBrainz match")
    else:
        _log(log_fn, "Disc ID unavailable (install python3-discid or cd-discid)")

    if not meta:
        _log(log_fn, "Trying CD-Text...")
        meta = read_cdtext(device)
        if meta:
            _log(log_fn, f"CD-Text: {meta['artist']} – {meta['album']}"
                         if meta.get("artist") or meta.get("album")
                         else "CD-Text: partial data found")
        else:
            _log(log_fn, "No CD-Text found (install libcdio-utils for CD-Text support)")
            _log(log_fn, "Tags will be prompted during import")

    if track_lengths:
        _log(log_fn, f"Disc: {len(track_lengths)} track(s) detected")
    _log(log_fn, "")

    # ── Rip with cdparanoia -B (batch mode, one WAV per track) ───────────────
    _log(log_fn, f"Ripping from {device} ...")
    current_track = 0
    cancelled = False

    try:
        proc = subprocess.Popen(
            ["cdparanoia", "-B", "-d", device],
            cwd=str(dest_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.stdout:
            for line in proc.stdout:
                if cancel_event and cancel_event.is_set():
                    proc.kill()
                    cancelled = True
                    break

                line = line.rstrip()
                if not line:
                    continue

                # Sector-progress counter — parse for the bar, don't log
                m = _SECTOR_RE.match(line.lstrip())
                if m:
                    if progress_fn and current_track > 0:
                        sector = int(m.group(1))
                        if track_lengths and current_track <= len(track_lengths):
                            total_s = max(1, track_lengths[current_track - 1])
                            pct = min(100, int(100 * sector / total_s))
                        else:
                            pct = 0
                        progress_fn(current_track,
                                    len(track_lengths) or current_track,
                                    pct)
                    continue

                # Track-start line
                m = _TRACK_RE.search(line)
                if m:
                    current_track = int(m.group(1))
                    _log(log_fn, f"  Ripping track {current_track}"
                         + (f"/{len(track_lengths)}" if track_lengths else "") + "...")
                    if progress_fn:
                        progress_fn(current_track,
                                    len(track_lengths) or current_track,
                                    0)
                    continue

                if "cdparanoia" not in line.lower():
                    _log(log_fn, f"  {line}")

        rc = proc.wait()
    except Exception as e:
        _log(log_fn, f"ERROR: {e}")
        return False

    if cancelled:
        _log(log_fn, "Rip cancelled.")
        return False

    if rc != 0:
        _log(log_fn, f"WARNING: cdparanoia exited {rc} (some sectors may be damaged)")

    # ── Convert WAV → FLAC and write tags ─────────────────────────────────────
    wavs = sorted(dest_dir.glob("*.wav"))
    if not wavs:
        _log(log_fn, "ERROR: no WAV files produced by cdparanoia")
        return False

    total = len(wavs)
    _log(log_fn, f"\nConverting {total} track(s) WAV → FLAC...")
    flacs: list[Path] = []

    for i, wav in enumerate(wavs, 1):
        if cancel_event and cancel_event.is_set():
            _log(log_fn, "Conversion cancelled.")
            break

        m = re.search(r"(\d+)", wav.stem)
        track_num = int(m.group(1)) if m else i
        flac = dest_dir / f"track{track_num:02d}.flac"
        _log(log_fn, f"  {wav.name} → {flac.name}")

        if progress_fn:
            progress_fn(i, total, -1)

        if _wav_to_flac(wav, flac, log_fn):
            wav.unlink()
            if meta:
                _write_flac_tags(flac, track_num, total, meta)
            flacs.append(flac)
        else:
            _log(log_fn, f"  ERROR converting {wav.name}")

    if not flacs:
        _log(log_fn, "ERROR: no FLAC files produced")
        return False

    _log(log_fn, f"\nRip complete: {len(flacs)}/{total} track(s) ready for import")
    return True
