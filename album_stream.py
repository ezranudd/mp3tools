#!/usr/bin/env python3
"""
Gapless album streaming for the web player's mobile "stream mode".

The mobile <audio> backend gaps between tracks because each .src swap reloads,
and because an <audio> element can't trim the MP3 encoder delay/padding silence
baked into every file. This module sidesteps both by concatenating a whole album
into ONE continuous, gapless PCM/WAV stream the browser plays as a single
resource: no per-track reload, and the encoder delay/padding is dropped as the
tracks are joined (ffmpeg honours the LAME header when decoding).

PCM is the default because it is lossless (no generation loss), gapless without
any frame surgery, and — being constant-rate — makes HTTP Range seeking pure
arithmetic (byte offset ↔ time is linear), which the client shim relies on to
fake per-track seek/next/prev over the single stream. For remote/cellular
clients iter_encoded() serves the same gapless PCM re-encoded to Opus (or MP3
for clients that can't play it) at a fraction of the bandwidth; that live
stream is not byte-seekable, so the client seeks it by reopening at a new start
second. Because iOS handles unbounded streams poorly (no lock-screen duration,
restarts instead of resuming when the locked connection drops), transcodes are
also written through to a per-library cache (start_cache_encode / cache_ready);
once cached, the server serves the file bounded + Range-seekable, giving the
transcoded stream the same reliability as the WAV one.

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
import threading
from pathlib import Path

from mutagen.mp3 import MP3

from mp3header import lame_delay_padding as _lame_delay_padding

_FRAME_SAMPLES = 1152          # samples per MPEG-1 Layer III frame
_BITS = 16
_WAV_HEADER_SIZE = 44
_CHUNK = 1 << 16               # 64 KiB PCM read granularity

# album_dir(str) -> (signature, layout). Recomputing the signature is a few
# stat()s; the layout itself (which reads every file's header) is the cached part.
_CACHE: dict[str, tuple] = {}


# ── Per-track gapless sample count ────────────────────────────────────────────

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
    """Canonical 44-byte PCM WAV header with the exact concatenated data size.

    The RIFF/data size fields are 32-bit; an album past ~6.7 h of stereo 44.1k
    PCM overflows them. Clamp to 0xFFFFFFFF (the conventional "unknown/max"
    fill) instead of crashing — range math uses layout()'s exact ints, not
    these header fields."""
    rate, channels = lay["sample_rate"], lay["channels"]
    byte_rate = rate * channels * (_BITS // 8)
    block_align = channels * (_BITS // 8)
    data_size = lay["data_bytes"]
    return b"".join((
        b"RIFF", struct.pack("<I", min(36 + data_size, 0xFFFFFFFF)), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, channels, rate,
                             byte_rate, block_align, _BITS),
        b"data", struct.pack("<I", min(data_size, 0xFFFFFFFF)),
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


# Encoded-stream variants for remote/cellular clients. Opus in Ogg is preferred
# (best quality per bit, native VBR, degrades gracefully as a second lossy
# generation) — but iOS's Ogg demuxer seeks/durations unreliably, so Apple
# devices get "aac" (MP4 carries a full sample table → sample-accurate seeks)
# or, experimentally, "caf" (the same Opus packets in Apple's own container,
# which also has a packet table). "mp3" is the universal fallback.
#
# Some containers need a seekable output (CAF's variable packet table, MP4's
# faststart moov), so each codec has separate args for the live piped stream
# vs the cache file: aac pipes raw ADTS but caches .m4a; caf can't pipe at all,
# so its live phase streams Ogg (same Opus packets) and only the cache is CAF.

def _codec_args(codec: str, bitrate: int, to_file: bool) -> list[str]:
    if codec == "opus":
        return ["-c:a", "libopus", "-b:a", f"{bitrate}k", "-f", "ogg"]
    if codec == "caf":
        fmt = "caf" if to_file else "ogg"
        return ["-c:a", "libopus", "-b:a", f"{bitrate}k", "-f", fmt]
    if codec == "aac":
        args = ["-c:a", "aac", "-b:a", f"{bitrate}k"]
        if to_file:
            return args + ["-movflags", "+faststart", "-f", "ipod"]
        return args + ["-f", "adts"]
    if codec == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-f", "mp3"]
    raise KeyError(codec)

_HAS_LIBOPUS: bool | None = None


def has_libopus() -> bool:
    """Whether this ffmpeg build can encode Opus (probed once)."""
    global _HAS_LIBOPUS
    if _HAS_LIBOPUS is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=10)
            _HAS_LIBOPUS = " libopus " in out.stdout
        except Exception:
            _HAS_LIBOPUS = False
    return _HAS_LIBOPUS


def iter_encoded(album_dir: Path, start_sec: float, codec: str, bitrate: int):
    """One continuous encoded stream (see _codec_args) of the album from
    *start_sec* to the end, encoded on the fly from the same gapless PCM that
    iter_range() serves — so it is just as gapless, at a fraction of the WAV
    stream's bandwidth. (ffmpeg auto-resamples 44.1k→48k for libopus; libopus
    runs in its default VBR mode since nothing does byte↔time math on this
    stream.)

    The output is NOT byte-seekable (an encoder can't resume mid-stream); the
    client seeks by reopening the stream at a new start_sec. Alignment to a
    whole sample keeps the raw s16le feed frame-exact."""
    lay = layout(album_dir)
    start_sample = min(max(0, round(start_sec * lay["sample_rate"])),
                       lay["total_samples"])
    start = _WAV_HEADER_SIZE + start_sample * lay["bytes_per"]
    end = lay["content_length"] - 1
    if start > end:
        return
    enc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error",
         "-f", "s16le", "-ar", str(lay["sample_rate"]),
         "-ac", str(lay["channels"]), "-i", "pipe:0",
         *_codec_args(codec, bitrate, to_file=False), "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)

    def _feed() -> None:
        try:
            for chunk in iter_range(album_dir, start, end):
                enc.stdin.write(chunk)
        except Exception:
            pass                # encoder gone (client disconnected) — stop feeding
        finally:
            try:
                enc.stdin.close()
            except Exception:
                pass

    threading.Thread(target=_feed, daemon=True).start()
    try:
        while True:
            buf = enc.stdout.read(_CHUNK)
            if not buf:
                break
            yield buf
    finally:
        enc.stdout.close()
        enc.terminate()
        try:
            enc.wait(timeout=5)
        except Exception:
            enc.kill()


# ── Transcode cache ───────────────────────────────────────────────────────────
# A finished encode is a bounded, Range-seekable file — which is what iOS needs
# for reliable locked-screen playback, lock-screen duration, and scrubbing (an
# unbounded chunked stream restarts from its opening byte when the suspended
# connection drops, and reports Infinity duration). The first play of an album
# streams live from the encoder while the cache fills (encoding runs far faster
# than realtime); every later request is served from the file.

_CACHE_EXT = {"opus": ".ogg", "mp3": ".mp3", "aac": ".m4a", "caf": ".caf"}
_CACHE_MAX_BYTES = 2 << 30            # prune oldest cache files past 2 GiB
_ENCODING: set[str] = set()           # cache keys with an encode in flight
_ENCODING_LOCK = threading.Lock()
# Album-open pre-warming can request several encodes in quick succession while
# the user browses; cap the ffmpeg processes and let the rest queue.
_ENCODE_SLOTS = threading.Semaphore(2)


def cache_path(cache_dir: Path, album_dir: Path, codec: str, bitrate: int) -> Path:
    """Cache file for this exact (album contents, codec, bitrate). The album's
    track signature is part of the key, so editing the album simply strands the
    old file for LRU pruning rather than serving stale audio."""
    import hashlib
    sig = _signature(sorted(album_dir.glob("*.mp3")))
    key = hashlib.sha1(repr((str(album_dir), codec, bitrate, sig)).encode()).hexdigest()
    return cache_dir / f"{key}{_CACHE_EXT[codec]}"


def cache_ready(cache_dir: Path, album_dir: Path, codec: str, bitrate: int) -> bool:
    return cache_path(cache_dir, album_dir, codec, bitrate).is_file()


def prune_cache(cache_dir: Path, max_bytes: int = _CACHE_MAX_BYTES) -> None:
    """Drop least-recently-served cache files until the dir fits max_bytes.
    (.part files are in-flight encodes — never touched.)"""
    try:
        files = [p for p in cache_dir.iterdir()
                 if p.is_file() and p.suffix != ".part"]
        files.sort(key=lambda p: p.stat().st_mtime)      # oldest first
        total = sum(p.stat().st_size for p in files)
        while total > max_bytes and files:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink()
    except OSError:
        pass


def start_cache_encode(cache_dir: Path, album_dir: Path, codec: str,
                       bitrate: int) -> None:
    """Kick off (or no-op if done/in-flight) a background encode of the album
    into the cache. ffmpeg writes to a real file here, so — unlike the piped
    live stream — it can seek back and finalize headers (MP3 Xing frame count,
    Ogg granule end), which is what gives players an exact duration."""
    final = cache_path(cache_dir, album_dir, codec, bitrate)
    if final.is_file():
        return
    key = str(final)
    with _ENCODING_LOCK:
        if key in _ENCODING:
            return
        _ENCODING.add(key)

    lay = layout(album_dir)

    def _encode() -> None:
        part = final.with_suffix(final.suffix + ".part")
        try:
            with _ENCODE_SLOTS:
                cache_dir.mkdir(parents=True, exist_ok=True)
                enc = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-v", "error", "-y",
                     "-f", "s16le", "-ar", str(lay["sample_rate"]),
                     "-ac", str(lay["channels"]), "-i", "pipe:0",
                     *_codec_args(codec, bitrate, to_file=True), str(part)],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    for chunk in iter_range(album_dir, _WAV_HEADER_SIZE,
                                            lay["content_length"] - 1):
                        enc.stdin.write(chunk)
                    enc.stdin.close()
                    if enc.wait(timeout=600) == 0 and part.stat().st_size > 0:
                        part.replace(final)
                        prune_cache(cache_dir)
                finally:
                    if enc.poll() is None:
                        enc.kill()
        except Exception:
            pass
        finally:
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            with _ENCODING_LOCK:
                _ENCODING.discard(key)

    threading.Thread(target=_encode, daemon=True).start()


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
