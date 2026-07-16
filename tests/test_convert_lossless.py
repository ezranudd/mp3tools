"""
Tests for convert_lossless.py. The CUE helpers and file discovery are pure;
codec-sensitive tests (is_alac, has_lame_header, conversion) carry markers.
"""
import pytest

from convert_lossless import _cue_to_secs, find_cue, find_lossless, parse_cue

from conftest import make_flac, make_m4a


# ── _cue_to_secs (pure) ───────────────────────────────────────────────────────

def test_cue_to_secs():
    assert _cue_to_secs("00:00:00") == 0.0
    assert _cue_to_secs("03:21:45") == pytest.approx(201.6)   # 3*60+21+45/75
    assert _cue_to_secs(" 1:2:3 ") == pytest.approx(62.04)    # whitespace + no padding


# ── find_cue (plain files) ────────────────────────────────────────────────────

def test_find_cue_same_stem_preferred(tmp_path):
    flac = tmp_path / "album.flac"
    flac.touch()
    other = tmp_path / "aaa.cue"        # sorts before album.cue
    other.touch()
    same = tmp_path / "album.cue"
    same.touch()
    assert find_cue(flac) == same


def test_find_cue_falls_back_to_first_in_folder(tmp_path):
    flac = tmp_path / "album.flac"
    flac.touch()
    (tmp_path / "zzz.cue").touch()
    (tmp_path / "aaa.cue").touch()
    assert find_cue(flac) == tmp_path / "aaa.cue"


def test_find_cue_none(tmp_path):
    flac = tmp_path / "album.flac"
    flac.touch()
    assert find_cue(flac) is None


# ── find_lossless ─────────────────────────────────────────────────────────────

def test_find_lossless_flac_and_alac_extensions(tmp_path):
    (tmp_path / "a.flac").touch()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.alac").touch()
    (tmp_path / "c.mp3").touch()
    found = find_lossless(tmp_path)
    assert [p.name for p in found] == ["a.flac", "b.alac"]


def test_find_lossless_unreadable_m4a_assumed_lossless(tmp_path):
    # is_alac() returns True on read error (conservative), so a bogus .m4a
    # is still treated as lossless rather than silently skipped.
    (tmp_path / "fake.m4a").write_bytes(b"not a real mp4")
    assert [p.name for p in find_lossless(tmp_path)] == ["fake.m4a"]


@pytest.mark.ffmpeg
def test_find_lossless_skips_aac_m4a(tmp_path):
    make_m4a(tmp_path / "lossy.m4a", codec="aac")
    make_m4a(tmp_path / "lossless.m4a", codec="alac")
    assert [p.name for p in find_lossless(tmp_path)] == ["lossless.m4a"]


@pytest.mark.ffmpeg
def test_is_alac(tmp_path):
    from convert_lossless import is_alac
    make_m4a(tmp_path / "yes.m4a", codec="alac")
    make_m4a(tmp_path / "no.m4a", codec="aac")
    assert is_alac(tmp_path / "yes.m4a") is True
    assert is_alac(tmp_path / "no.m4a") is False


# ── has_lame_header (gapless-critical) ────────────────────────────────────────

@pytest.mark.ffmpeg
def test_has_lame_header_false_for_ffmpeg_encode(tmp_path):
    # ffmpeg's own libmp3lame muxer writes dummy encoder delay/padding
    # (0xAAA/0x555) — exactly what has_lame_header must reject.
    import subprocess
    from convert_lossless import has_lame_header
    src = tmp_path / "a.flac"
    make_flac(src)
    dst = tmp_path / "a.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i",
                    str(src), "-c:a", "libmp3lame", "-b:a", "192k", str(dst)],
                   check=True)
    assert has_lame_header(dst) is False


@pytest.mark.ffmpeg
@pytest.mark.lame
def test_has_lame_header_true_for_lame_encode(tmp_path):
    from convert_lossless import _lame_pipe_convert, has_lame_header
    src = tmp_path / "a.flac"
    make_flac(src)
    dst = tmp_path / "a.mp3"
    assert _lame_pipe_convert(src, dst, 192) is True
    assert has_lame_header(dst) is True


def test_has_lame_header_garbage_file(tmp_path):
    from convert_lossless import has_lame_header
    p = tmp_path / "junk.mp3"
    p.write_bytes(b"\x00" * 256)
    assert has_lame_header(p) is False


# ── parse_cue (pure) ──────────────────────────────────────────────────────────

_CUE = '''\
REM GENRE "Rock"
REM DATE 1994-05-01
PERFORMER "Album Artist"
TITLE "Album Title"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First Song"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Song"
    PERFORMER "Guest Artist"
    INDEX 01 03:21:45
  TRACK 03 AUDIO
    TITLE "No Index"
'''


def test_parse_cue(tmp_path):
    cue = tmp_path / "album.cue"
    cue.write_text(_CUE, encoding="utf-8")
    tracks = parse_cue(cue)

    # Track 03 has no INDEX 01 → filtered out as malformed.
    assert [t["track_num"] for t in tracks] == [1, 2]

    t1, t2 = tracks
    assert t1["title"] == "First Song"
    assert t1["artist"] == "Album Artist"       # inherits album performer
    assert t1["start_secs"] == 0.0
    assert t1["end_secs"] == pytest.approx(201.6)

    assert t2["artist"] == "Guest Artist"       # per-track performer wins
    assert t2["end_secs"] is None               # last track runs to EOF

    for t in tracks:
        assert t["album_artist"] == "Album Artist"
        assert t["album_title"] == "Album Title"
        assert t["album_year"] == "1994"
        assert t["album_genre"] == "Rock"


def test_parse_cue_missing_file(tmp_path):
    assert parse_cue(tmp_path / "absent.cue") == []


# ── _decode_args: channel/rate/bit-depth guards for the encode pipe ──────────

def _lavfi_flac(path, cl="stereo", rate=44100, extra=()):
    """A FLAC straight from ffmpeg with a chosen channel layout / rate."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={cl}",
         "-t", "0.5", *extra, str(path)],
        check=True,
    )


@pytest.mark.ffmpeg
def test_decode_args_plain_stereo_is_empty(tmp_path):
    from convert_lossless import _decode_args
    src = tmp_path / "t.flac"
    make_flac(src)
    assert _decode_args(src) == []


@pytest.mark.ffmpeg
def test_decode_args_downmixes_multichannel(tmp_path):
    from convert_lossless import _decode_args
    src = tmp_path / "t51.flac"
    _lavfi_flac(src, cl="5.1")
    args = _decode_args(src)
    assert args[args.index("-ac") + 1] == "2"


@pytest.mark.ffmpeg
def test_decode_args_clamps_high_rates(tmp_path):
    from convert_lossless import _decode_args
    src96 = tmp_path / "t96.flac"
    _lavfi_flac(src96, rate=96000)
    args = " ".join(_decode_args(src96))
    assert "48000" in args

    src882 = tmp_path / "t882.flac"
    _lavfi_flac(src882, rate=88200)
    args = " ".join(_decode_args(src882))
    assert "44100" in args               # 88.2k family → 44.1k, not 48k


@pytest.mark.ffmpeg
def test_decode_args_dithers_24bit(tmp_path):
    from mutagen.flac import FLAC
    from convert_lossless import _decode_args
    src = tmp_path / "t24.flac"
    _lavfi_flac(src, extra=("-sample_fmt", "s32"))
    assert FLAC(src).info.bits_per_sample > 16, "fixture must be >16-bit"
    args = " ".join(_decode_args(src))
    assert "dither_method" in args and "osf=s16" in args


@pytest.mark.ffmpeg
@pytest.mark.lame
def test_convert_downmixes_51_to_stereo(tmp_path):
    from mutagen.mp3 import MP3
    from convert_lossless import convert_to_mp3
    src = tmp_path / "t51.flac"
    _lavfi_flac(src, cl="5.1")
    dst = tmp_path / "t51.mp3"
    assert convert_to_mp3(src, dst, 192) is True   # lame can't take 5.1 raw
    assert MP3(dst).info.channels == 2


# ── ffmpeg fallback path: no v2.4 relics ─────────────────────────────────────

@pytest.mark.ffmpeg
def test_ffmpeg_fallback_leaves_no_tdrc(tmp_path, monkeypatch):
    """Without lame, the ffmpeg muxer tags the file itself; the re-tag pass
    must not let a TDRC (ID3v2.4 timestamp) survive in the v2.3 output."""
    import convert_lossless as cl
    from mutagen.id3 import ID3
    monkeypatch.setattr(cl, "_has_lame_binary", lambda: False)
    src = tmp_path / "t.flac"
    make_flac(src, title="Song", artist="Band", date="2019-06-01")
    dst = tmp_path / "t.mp3"
    assert cl.convert_to_mp3(src, dst, 192) is True
    t = ID3(dst, translate=False)
    assert "TDRC" not in t
    assert str(t["TYER"]) == "2019"
    assert t.version[1] == 3
