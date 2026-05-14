#!/usr/bin/env python3
"""
Rip a CD to FLAC files with stub tags from MusicBrainz.
The resulting directory can be passed to import_tracks.import_tracks().

External requirements (all optional — gracefully degraded):
  cdparanoia   sudo apt install cdparanoia
  ffmpeg       sudo apt install ffmpeg
  cd-discid    sudo apt install cd-discid   (for disc ID + CDDB lookup)
  python-discid pip install discid          (for MusicBrainz lookup, preferred)
  musicbrainzngs pip install musicbrainzngs (for MusicBrainz metadata)
"""

import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ── Device detection ──────────────────────────────────────────────────────────

_CD_DEVICE_PATHS = ["/dev/cdrom", "/dev/dvd", "/dev/sr0", "/dev/sr1", "/dev/sr2"]


def detect_cd_devices() -> list[Path]:
    """Return paths to optical drive block devices that exist on this system."""
    return [Path(d) for d in _CD_DEVICE_PATHS if Path(d).exists()]


def eject_device(device: str | Path) -> None:
    """Eject the disc from `device` using the system eject command."""
    try:
        subprocess.run(["eject", str(device)], timeout=10)
    except Exception:
        pass


# ── Disc TOC (ID + track lengths) ────────────────────────────────────────────

def read_disc_toc(device: str | Path) -> tuple[str | None, dict | None, list[int]]:
    """
    Read the disc table of contents.
    Returns (mb_disc_id, cddb_toc, track_lengths_in_sectors).
    - mb_disc_id: MusicBrainz disc ID (only available via python-discid)
    - cddb_toc: dict with 'disc_id', 'offsets', 'total_seconds' for gnudb lookups
    - track_lengths: list of per-track lengths in sectors
    Any value may be None/empty if unavailable.
    """
    device = str(device)

    # python-discid gives us the MB disc ID, CDDB ID, offsets, and track lengths
    try:
        import discid  # type: ignore
        disc = discid.read(device)
        lengths = [t.length for t in disc.tracks]
        offsets = [t.offset for t in disc.tracks]
        total_seconds = disc.sectors // 75
        cddb_toc = {
            "disc_id": disc.freedb_id,
            "offsets": offsets,
            "total_seconds": total_seconds,
        }
        return disc.id, cddb_toc, lengths
    except ImportError:
        pass
    except Exception:
        pass

    # No discid — parse the full cd-discid output for CDDB TOC data
    cddb_toc: dict | None = None
    try:
        result = subprocess.run(
            ["cd-discid", device],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            # format: <discid> <numtracks> <offset1> ... <offsetN> <total_seconds>
            if len(parts) >= 3:
                disc_id = parts[0]
                num_tracks = int(parts[1])
                offsets = [int(x) for x in parts[2:2 + num_tracks]]
                total_seconds = int(parts[2 + num_tracks]) if len(parts) > 2 + num_tracks else 0
                cddb_toc = {"disc_id": disc_id, "offsets": offsets, "total_seconds": total_seconds}
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

    return None, cddb_toc, track_lengths


# ── CD-Text ───────────────────────────────────────────────────────────────────

def read_cdtext(device: str | Path) -> dict | None:
    """
    Read CD-Text from the disc using cd-info (sudo apt install libcdio-utils).
    Returns {"artist", "album", "year", "genre", "tracks": [title, ...]}, or None
    if cd-info is not installed or the disc carries no CD-Text.
    """
    try:
        result = subprocess.run(
            ["cd-info", "-q", str(device)],
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


# ── gnudb (CDDB) lookup ───────────────────────────────────────────────────────

_CDDB_SERVERS = [
    ("gnudb",       "http://gnudb.gnudb.org/~cddb/cddb.cgi"),
    ("MusicBrainz", "http://freedb.musicbrainz.org/~cddb/cddb.cgi"),
]

_CDDB_HELLO = "hello=anonymous+localhost+mp3tools+1.0"
_CDDB_PROTO = "proto=6"


def _cddb_query_server(base: str, disc_id: str, offsets: list[int],
                       total_seconds: int) -> dict | None:
    """
    Run a CDDB query+read against a single server base URL.
    Returns a metadata dict or None if the disc is not found or an error occurs.
    """
    offset_str = "+".join(str(o) for o in offsets)
    query_cmd  = f"cddb+query+{disc_id}+{len(offsets)}+{offset_str}+{total_seconds}"
    query_url  = f"{base}?cmd={query_cmd}&{_CDDB_HELLO}&{_CDDB_PROTO}"

    try:
        with urllib.request.urlopen(query_url, timeout=10) as resp:
            query_result = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    lines = query_result.splitlines()
    if not lines:
        return None

    code = lines[0].split()[0] if lines[0].split() else ""
    if code == "200":
        parts = lines[0].split(None, 3)
        category, found_id = parts[1], parts[2]
    elif code in ("210", "211"):
        if len(lines) < 2:
            return None
        parts = lines[1].split(None, 2)
        category, found_id = parts[0], parts[1]
    else:
        return None

    read_cmd = f"cddb+read+{category}+{found_id}"
    read_url = f"{base}?cmd={read_cmd}&{_CDDB_HELLO}&{_CDDB_PROTO}"
    try:
        with urllib.request.urlopen(read_url, timeout=10) as resp:
            read_result = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    dtitle = dyear = dgenre = ""
    track_titles: list[tuple[int, str]] = []
    for line in read_result.splitlines():
        if line.startswith("#") or line.strip() in (".", ""):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key == "DTITLE":
            dtitle = val
        elif key == "DYEAR":
            dyear = val
        elif key == "DGENRE":
            dgenre = val
        elif key.startswith("TTITLE"):
            try:
                idx = int(key[6:])
                track_titles.append((idx, val))
            except ValueError:
                pass

    if not dtitle:
        return None

    if " / " in dtitle:
        artist, _, album = dtitle.partition(" / ")
    else:
        artist, album = "", dtitle

    track_titles.sort(key=lambda x: x[0])
    tracks = [t for _, t in track_titles]
    return {"artist": artist.strip(), "album": album.strip(),
            "year": dyear, "genre": dgenre or category, "tracks": tracks}


def lookup_gnudb(cddb_toc: dict) -> dict | None:
    """
    Look up disc metadata via CDDB, trying each server in _CDDB_SERVERS in order.
    cddb_toc must have keys: disc_id, offsets (list of ints), total_seconds (int).
    Returns {"artist", "album", "year", "genre", "tracks", "_server"}, or None.
    """
    disc_id      = cddb_toc.get("disc_id", "")
    offsets      = cddb_toc.get("offsets", [])
    total_seconds = cddb_toc.get("total_seconds", 0)
    if not disc_id or not offsets:
        return None

    for label, base in _CDDB_SERVERS:
        result = _cddb_query_server(base, disc_id, offsets, total_seconds)
        if result:
            result["_server"] = label
            return result
    return None


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
# Matches the file-output announcement cdparanoia makes per track:
# "outputting to cdda2wav track 01.cdda.wav"  — only fires on the .cdda filename,
# not on the TOC listing lines like "Track  1: sector 0 to ..."
_TRACK_START_RE = re.compile(r"track\s*0*(\d+)\.cdda", re.IGNORECASE)


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

    # ── Disc TOC, MusicBrainz, gnudb, and CD-Text metadata ───────────────────
    _log(log_fn, "Reading disc...")
    mb_disc_id, cddb_toc, track_lengths = read_disc_toc(device)
    meta: dict | None = None

    if cddb_toc:
        _log(log_fn, f"Disc ID: {cddb_toc['disc_id']}")
    elif not mb_disc_id:
        _log(log_fn, "Disc ID unavailable (install python3-discid or cd-discid)")

    if mb_disc_id:
        _log(log_fn, "Looking up MusicBrainz...")
        meta = lookup_musicbrainz(mb_disc_id)
        if meta:
            suffix = f" ({meta['year']})" if meta.get("year") else ""
            _log(log_fn, f"MusicBrainz: {meta['artist']} – {meta['album']}{suffix}")
        else:
            _log(log_fn, "No MusicBrainz match")

    if not meta and cddb_toc:
        _log(log_fn, "Looking up CDDB...")
        meta = lookup_gnudb(cddb_toc)
        if meta:
            suffix = f" ({meta['year']})" if meta.get("year") else ""
            server = meta.get("_server", "CDDB")
            _log(log_fn, f"{server}: {meta['artist']} – {meta['album']}{suffix}")
        else:
            _log(log_fn, "No CDDB match")

    if not meta:
        _log(log_fn, "Trying CD-Text...")
        meta = read_cdtext(device)
        if meta:
            _log(log_fn, f"CD-Text: {meta['artist']} – {meta['album']}"
                         if meta.get("artist") or meta.get("album")
                         else "CD-Text: partial data found")
        else:
            _log(log_fn, "No CD-Text on disc")
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

                # Sector-progress counter — update log bar, don't emit a new line
                m = _SECTOR_RE.match(line.lstrip())
                if m:
                    if progress_fn and current_track > 0 and track_lengths:
                        sector  = int(m.group(1))
                        total_s = max(1, track_lengths[current_track - 1])
                        pct     = min(100, int(100 * sector / total_s))
                        if pct > 0:
                            progress_fn(current_track,
                                        len(track_lengths),
                                        pct)
                    continue

                # Track-start announcement ("outputting to track01.cdda.wav")
                m = _TRACK_START_RE.search(line)
                if m:
                    current_track = int(m.group(1))
                    total_t = len(track_lengths) or current_track
                    _log(log_fn, f"  Ripping track {current_track}/{total_t}...")
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
