"""
album_stream tests: WAV header overflow guard and the transcoded (codec=mp3)
gapless stream.
"""
import subprocess

import pytest
from mutagen.mp3 import MP3

import album_stream

from conftest import make_mp3


def test_wav_header_clamps_32bit_overflow():
    lay = {"sample_rate": 44100, "channels": 2,
           "data_bytes": 5 * 1024 ** 3}          # > 4 GiB — past the RIFF field
    hdr = album_stream.wav_header(lay)           # must not raise struct.error
    assert len(hdr) == 44
    assert hdr[:4] == b"RIFF"
    assert hdr[4:8] == b"\xff\xff\xff\xff"       # clamped, not wrapped
    assert hdr[40:44] == b"\xff\xff\xff\xff"


def _make_album(tmp_path):
    album = tmp_path / "Artist" / "2024 - Album"
    for i in (1, 2):
        make_mp3(album / f"0{i}. Artist - Song {i}.mp3", duration=1.5,
                 TIT2=f"Song {i}", TPE1="Artist", TPE2="Artist",
                 TALB="Album", TYER="2024", TRCK=f"0{i}/2")
    return album


@pytest.mark.ffmpeg
def test_iter_encoded_mp3_produces_decodable_stream(tmp_path):
    album = _make_album(tmp_path)

    data = b"".join(album_stream.iter_encoded(album, 0.0, "mp3", 192))
    out = tmp_path / "stream.mp3"
    out.write_bytes(data)

    info = MP3(out).info
    total = album_stream.manifest(album)["total_sec"]
    # Whole-album duration (allow the encoder's own delay/padding slack).
    assert info.length == pytest.approx(total, abs=0.2)
    assert 180_000 <= info.bitrate <= 205_000    # CBR 192k

    # Mid-stream start: shorter, still decodable.
    partial = b"".join(album_stream.iter_encoded(album, total / 2, "mp3", 192))
    out2 = tmp_path / "partial.mp3"
    out2.write_bytes(partial)
    assert MP3(out2).info.length == pytest.approx(total / 2, abs=0.2)

    # Past-the-end start yields nothing rather than erroring.
    assert b"".join(album_stream.iter_encoded(album, total + 10, "mp3", 192)) == b""


@pytest.mark.ffmpeg
def test_iter_encoded_opus_produces_decodable_stream(tmp_path):
    if not album_stream.has_libopus():
        pytest.skip("ffmpeg build has no libopus")
    from mutagen.oggopus import OggOpus

    album = _make_album(tmp_path)
    total = album_stream.manifest(album)["total_sec"]

    data = b"".join(album_stream.iter_encoded(album, 0.0, "opus", 160))
    out = tmp_path / "stream.ogg"
    out.write_bytes(data)
    assert data[:4] == b"OggS"
    assert OggOpus(out).info.length == pytest.approx(total, abs=0.2)

    partial = b"".join(album_stream.iter_encoded(album, total / 2, "opus", 96))
    out2 = tmp_path / "partial.ogg"
    out2.write_bytes(partial)
    assert OggOpus(out2).info.length == pytest.approx(total / 2, abs=0.2)


# ── Transcode cache (bounded serving is what makes iOS lock-screen playback,
#    duration, and scrubbing reliable) ─────────────────────────────────────────

def _wait_ready(cache_dir, album, codec, br, timeout=30):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if album_stream.cache_ready(cache_dir, album, codec, br):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.ffmpeg
def test_cache_encode_produces_bounded_decodable_file(tmp_path):
    album = _make_album(tmp_path)
    cache_dir = tmp_path / "cache"
    total = album_stream.manifest(album)["total_sec"]

    assert not album_stream.cache_ready(cache_dir, album, "mp3", 192)
    album_stream.start_cache_encode(cache_dir, album, "mp3", 192)
    assert _wait_ready(cache_dir, album, "mp3", 192), "encode did not finish"

    cached = album_stream.cache_path(cache_dir, album, "mp3", 192)
    info = MP3(cached).info
    # File output lets ffmpeg finalize the Xing header → exact duration, the
    # property the bounded stream depends on for lock-screen time/scrubbing.
    assert info.length == pytest.approx(total, abs=0.2)

    # Re-kick is a no-op (already cached).
    album_stream.start_cache_encode(cache_dir, album, "mp3", 192)
    assert cached.is_file()

    # Editing the album changes the key — the old cache is never served stale.
    import time
    time.sleep(0.02)
    next(iter(album.glob("*.mp3"))).touch()
    assert not album_stream.cache_ready(cache_dir, album, "mp3", 192)


def test_prune_cache_drops_oldest(tmp_path):
    import os
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    for i, name in enumerate(["a.ogg", "b.mp3", "c.ogg"]):
        p = cache_dir / name
        p.write_bytes(b"x" * 100)
        os.utime(p, (1000 + i, 1000 + i))          # a oldest … c newest
    (cache_dir / "d.ogg.part").write_bytes(b"x" * 1000)  # in-flight: untouchable

    album_stream.prune_cache(cache_dir, max_bytes=250)

    names = sorted(p.name for p in cache_dir.iterdir())
    assert "a.ogg" not in names                     # oldest evicted
    assert {"b.mp3", "c.ogg", "d.ogg.part"} <= set(names)


@pytest.mark.ffmpeg
def test_aac_live_and_cached_variants(tmp_path):
    """AAC pipes raw ADTS live and caches a faststart .m4a with exact duration
    (the Apple-reliable variant: MP4's sample table gives exact seeks)."""
    from mutagen.mp4 import MP4

    album = _make_album(tmp_path)
    total = album_stream.manifest(album)["total_sec"]

    live = b"".join(album_stream.iter_encoded(album, 0.0, "aac", 192))
    assert live[0] == 0xFF and (live[1] & 0xF0) == 0xF0    # ADTS sync word

    cache_dir = tmp_path / "cache"
    album_stream.start_cache_encode(cache_dir, album, "aac", 192)
    assert _wait_ready(cache_dir, album, "aac", 192)
    cached = album_stream.cache_path(cache_dir, album, "aac", 192)
    assert cached.suffix == ".m4a"
    assert MP4(cached).info.length == pytest.approx(total, abs=0.2)


@pytest.mark.ffmpeg
def test_caf_live_is_ogg_and_cache_is_caf(tmp_path):
    """CAF can't be muxed to a pipe, so codec=caf streams Ogg live and only the
    cached bounded file is CAF (Apple's Opus container, with a packet table)."""
    import subprocess
    if not album_stream.has_libopus():
        pytest.skip("ffmpeg build has no libopus")

    album = _make_album(tmp_path)
    total = album_stream.manifest(album)["total_sec"]

    live = b"".join(album_stream.iter_encoded(album, 0.0, "caf", 96))
    assert live[:4] == b"OggS"                             # live phase = Ogg

    cache_dir = tmp_path / "cache"
    album_stream.start_cache_encode(cache_dir, album, "caf", 96)
    assert _wait_ready(cache_dir, album, "caf", 96)
    cached = album_stream.cache_path(cache_dir, album, "caf", 96)
    assert cached.suffix == ".caf"
    assert cached.read_bytes()[:4] == b"caff"              # CAF magic
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name:format=duration", "-of", "csv=p=0", str(cached)],
        capture_output=True, text=True)
    lines = probe.stdout.split()
    assert "opus" in probe.stdout
    assert float(lines[-1]) == pytest.approx(total, abs=0.2)


@pytest.mark.ffmpeg
def test_concurrent_prewarm_encodes_all_complete(tmp_path):
    """Album-open pre-warming can kick several encodes at once; the semaphore
    queues them and every one must still finish."""
    albums = []
    for n in range(4):
        album = tmp_path / f"Artist {n}" / f"200{n} - Album {n}"
        make_mp3(album / f"01. Artist {n} - Song.mp3", duration=1.0,
                 TIT2="Song", TPE1=f"Artist {n}", TPE2=f"Artist {n}",
                 TALB=f"Album {n}", TYER=f"200{n}", TRCK="01/1")
        albums.append(album)

    cache_dir = tmp_path / "cache"
    for album in albums:
        album_stream.start_cache_encode(cache_dir, album, "mp3", 192)
    for album in albums:
        assert _wait_ready(cache_dir, album, "mp3", 192), f"{album.name} never cached"
