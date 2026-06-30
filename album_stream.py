#!/usr/bin/env python3
"""
Gapless album streaming for the web player's mobile "stream mode".

The mobile <audio> backend gaps between tracks because each .src swap reloads,
and because an <audio> element can't trim the MP3 encoder delay/padding silence
baked into every file. This module sidesteps both by concatenating a whole album
into ONE continuous, gapless PCM/WAV stream the browser plays as a single
resource: no per-track reload, and the encoder delay/padding is dropped as the
tracks are joined (ffmpeg honours the LAME header when decoding).

PCM is chosen over a re-encoded MP3 because it is lossless (no generation loss),
gapless without any frame surgery, and — being constant-rate — makes HTTP Range
seeking pure arithmetic (byte offset ↔ time is linear), which the client shim
relies on to fake per-track seek/next/prev over the single stream.

Public surface (used by server.py):
  layout(album_dir)   -> cached {sample_rate, channels, total_samples,
                          content_length, tracks:[...]}  (byte/sample offsets)
  manifest(album_dir) -> JSON-friendly per-track {title, artist, track,
                          start_sec, dur_sec} + total_sec  (drives the client shim)
  wav_header(layout)  -> the 44-byte canonical WAV header
  iter_range(album_dir, start, end) -> generator yielding bytes [start, end] of
                          the full WAV (header ++ concatenated PCM)
"""
from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from mutagen.mp3 import MP3

# ffmpeg's libmp3lame muxer fills delay/padding with these dummy patterns (see
# convert_lossless.has_lame_header); treat them as "no usable header".
_DUMMY_DELAY_PADDING = {(0x756, 0x554), (0x756, 0x555)}

_FRAME_SAMPLES = 1152          # samples per MPEG-1 Layer III frame
_BITS = 16
_WAV_HEADER_SIZE = 44
_CHUNK = 1 << 16               # 64 KiB PCM read granularity

# album_dir(str) -> (signature, layout). Recomputing the signature is a few
# stat()s; the layout itself (which reads every file's header) is the cached part.
_CACHE: dict[str, tuple] = {}


# ── Per-track gapless sample count ────────────────────────────────────────────

def _lame_delay_padding(path: Path) -> tuple[int, int] | None:
    """Encoder (delay, padding) in samples from the Xing/Info+LAME header, or
    None when absent/dummy. Mirrors convert_lossless.has_lame_header's parse."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(10)
            skip = 0
            if head[:3] == b"ID3":
                skip = 10 + ((head[6] << 21) | (head[7] << 14)
                             | (head[8] << 7) | head[9])
            fh.seek(skip)
            buf = fh.read(2048)
        if not (b"Xing" in buf or b"Info" in buf):
            return None
        j = buf.find(b"LAME")
        if j < 0 or j + 24 > len(buf):
            return None
        v = int.from_bytes(buf[j + 21:j + 24], "big")
        delay, padding = v >> 12, v & 0xFFF
        if (delay, padding) in _DUMMY_DELAY_PADDING:
            return None
        return delay, padding
    except Exception:
        return None


def _track_samples(path: Path, album_rate: int) -> int:
    """Exact gapless sample count (per channel) this track contributes to the
    stream, in the album's output sample rate.

    The full encoded length (mutagen's info.length, derived from the Xing frame
    count) includes the priming/padding silence; subtracting the LAME
    delay+padding gives the trimmed length ffmpeg actually decodes. Resample the
    count when a track's native rate differs from the album rate. A header-less
    file can't be trimmed (best effort: keep full length); the exact byte count
    is enforced again at stream time, so a small miscount can't desync the WAV."""
    try:
        info = MP3(path).info
        rate = info.sample_rate or album_rate
        total_incl = round(info.length * rate)
    except Exception:
        return 0
    dp = _lame_delay_padding(path)
    trimmed = total_incl - (dp[0] + dp[1]) if dp else total_incl
    if rate != album_rate and rate:
        trimmed = round(trimmed * album_rate / rate)
    return max(0, trimmed)


# ── Album layout (cached) ─────────────────────────────────────────────────────

def _signature(mp3s: list[Path]) -> tuple:
    out = []
    for p in mp3s:
        try:
            st = p.stat()
            out.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p.name, 0, 0))
    return tuple(out)


def layout(album_dir: Path) -> dict:
    """Concatenation layout for *album_dir*, cached until a track changes.

    Track order matches server.api_album: sorted(glob("*.mp3")). The output rate
    is the first track's native rate (albums are uniform in practice), so the
    common case needs no resampling."""
    import browse

    mp3s = sorted(album_dir.glob("*.mp3"))
    sig = _signature(mp3s)
    cached = _CACHE.get(str(album_dir))
    if cached and cached[0] == sig:
        return cached[1]

    album_rate = 44100
    if mp3s:
        try:
            album_rate = MP3(mp3s[0]).info.sample_rate or 44100
        except Exception:
            album_rate = 44100
    channels = 2
    bytes_per = channels * (_BITS // 8)

    tracks = []
    start_sample = 0
    for mp3 in mp3s:
        n = _track_samples(mp3, album_rate)
        tags = browse.read_tags(mp3)
        tracks.append({
            "path": str(mp3),
            "title": tags.get("title") or mp3.stem,
            "artist": tags.get("artist", ""),
            "track": tags.get("track", ""),
            "start_sample": start_sample,
            "n_samples": n,
            "start_byte": start_sample * bytes_per,
            "n_bytes": n * bytes_per,
        })
        start_sample += n

    total_samples = start_sample
    data_bytes = total_samples * bytes_per
    out = {
        "sample_rate": album_rate,
        "channels": channels,
        "bytes_per": bytes_per,
        "total_samples": total_samples,
        "data_bytes": data_bytes,
        "content_length": _WAV_HEADER_SIZE + data_bytes,
        "tracks": tracks,
    }
    _CACHE[str(album_dir)] = (sig, out)
    return out


def manifest(album_dir: Path) -> dict:
    """Per-track offsets for the client shim (seconds), same order as the stream."""
    lay = layout(album_dir)
    rate = lay["sample_rate"] or 44100
    tracks = [{
        "path": t["path"],
        "title": t["title"],
        "artist": t["artist"],
        "track": t["track"],
        "start_sec": t["start_sample"] / rate,
        "dur_sec": t["n_samples"] / rate,
    } for t in lay["tracks"]]
    return {
        "sample_rate": rate,
        "total_sec": lay["total_samples"] / rate,
        "content_length": lay["content_length"],
        "tracks": tracks,
    }


# ── WAV framing + PCM generation ──────────────────────────────────────────────

def wav_header(lay: dict) -> bytes:
    """Canonical 44-byte PCM WAV header with the exact concatenated data size."""
    rate, channels = lay["sample_rate"], lay["channels"]
    byte_rate = rate * channels * (_BITS // 8)
    block_align = channels * (_BITS // 8)
    data_size = lay["data_bytes"]
    return b"".join((
        b"RIFF", struct.pack("<I", 36 + data_size), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, rate,
                             byte_rate, block_align, _BITS),
        b"data", struct.pack("<I", data_size),
    ))


def _decode_track(path: str, rate: int, channels: int, target_bytes: int):
    """Yield this track's PCM as s16le at (rate, channels), normalised to exactly
    *target_bytes* — truncating ffmpeg's tail or zero-padding a short decode — so
    the concatenated stream's length always matches the WAV header. -map 0:a
    drops any cover-art video stream."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", path,
        "-map", "0:a", "-ac", str(channels), "-ar", str(rate),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    emitted = 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while emitted < target_bytes:
            chunk = proc.stdout.read(min(_CHUNK, target_bytes - emitted))
            if not chunk:
                break
            emitted += len(chunk)
            yield chunk
    finally:
        proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    if emitted < target_bytes:                       # short decode → pad silence
        yield b"\x00" * (target_bytes - emitted)


def iter_range(album_dir: Path, start: int, end: int):
    """Yield bytes [start, end] (inclusive) of the full WAV (header ++ PCM).

    Walks the logical segments (header, then each track) and emits only the
    portion overlapping the requested window. The first overlapped track is
    decoded from its start and its prefix discarded — at most one track of wasted
    decode per seek, which keeps the byte math exact without fragile -ss seeking."""
    lay = layout(album_dir)
    end = min(end, lay["content_length"] - 1)
    if start > end:
        return

    pos = start
    # Header segment [0, 44).
    if pos < _WAV_HEADER_SIZE:
        hdr = wav_header(lay)
        stop = min(_WAV_HEADER_SIZE - 1, end)
        yield hdr[pos:stop + 1]
        pos = stop + 1
        if pos > end:
            return

    # Data segments. Track byte ranges are in data coordinates (0 = first PCM
    # byte); shift the request window into the same frame by subtracting 44.
    req_lo = pos - _WAV_HEADER_SIZE
    req_hi = end - _WAV_HEADER_SIZE
    for t in lay["tracks"]:
        seg_lo = t["start_byte"]
        seg_hi = seg_lo + t["n_bytes"] - 1
        if t["n_bytes"] <= 0 or seg_hi < req_lo or seg_lo > req_hi:
            continue
        want_lo = max(req_lo, seg_lo) - seg_lo      # track-local byte window
        want_hi = min(req_hi, seg_hi) - seg_lo
        local = 0
        for chunk in _decode_track(t["path"], lay["sample_rate"],
                                   lay["channels"], t["n_bytes"]):
            c_lo, c_hi = local, local + len(chunk) - 1
            local += len(chunk)
            if c_hi < want_lo:
                continue
            if c_lo > want_hi:
                break
            a = max(want_lo, c_lo) - c_lo
            b = min(want_hi, c_hi) - c_lo
            yield chunk[a:b + 1]
