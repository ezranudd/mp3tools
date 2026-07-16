"""
Encoding-profile registry + the encoder functions in convert_lossless.

Registry tests are pure. Encode tests build a real FLAC and run the installed
encoder, so they carry @pytest.mark.ffmpeg / .lame / .opus and auto-skip when a
binary/libopus is missing.
"""
import subprocess

import pytest

import encoding
from encoding import DEFAULT_PROFILE, PROFILES

from conftest import make_flac


# ── Registry (pure) ───────────────────────────────────────────────────────────

def test_registry_profiles_in_order():
    assert list(PROFILES) == ["opus-192", "opus-160", "opus-128", "opus-96",
                              "opus-64", "mp3-v0", "mp3-320"]


def test_default_is_opus_128():
    assert DEFAULT_PROFILE == "opus-128"
    assert encoding.get(DEFAULT_PROFILE).fmt == "opus"


def test_get_falls_back_to_default_on_unknown():
    assert encoding.get("nope").id == DEFAULT_PROFILE
    assert encoding.get(None).id == DEFAULT_PROFILE


def test_profile_shapes():
    assert PROFILES["opus-128"].opus_bitrate == 128
    assert PROFILES["opus-128"].ext == ".opus"
    assert PROFILES["mp3-v0"].lame_args == ("-V", "0")
    assert PROFILES["mp3-320"].lame_args == ("--cbr", "-b", "320")
    assert PROFILES["mp3-320"].ext == ".mp3"


def test_profiles_json_is_ordered_id_fmt_label():
    js = encoding.profiles_json()
    assert [p["id"] for p in js] == list(PROFILES)
    assert set(js[0]) == {"id", "fmt", "label"}


def test_legacy_bitrate_migration_map():
    assert encoding.LEGACY_BITRATE_TO_PROFILE[320] == "mp3-320"
    assert all(encoding.LEGACY_BITRATE_TO_PROFILE[b] == "mp3-v0"
               for b in (256, 192, 160, 128))


# ── Probe helpers ─────────────────────────────────────────────────────────────

def _codec(path) -> str:
    r = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    return r.stdout.strip()


def _channels(path) -> int:
    r = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    return int(r.stdout.strip())


def _mono_flac(path):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.5", str(path)],
        check=True)


# ── MP3 profiles: real LAME gapless header ────────────────────────────────────

@pytest.mark.ffmpeg
@pytest.mark.lame
@pytest.mark.parametrize("pid", ["mp3-v0", "mp3-320"])
def test_mp3_profiles_carry_lame_header(tmp_path, pid):
    from convert_lossless import convert_audio, has_lame_header
    src = tmp_path / "a.flac"
    make_flac(src, title="Song", artist="Band")
    dst = tmp_path / f"{pid}.mp3"
    assert convert_audio(src, dst, PROFILES[pid]) is True
    assert _codec(dst) == "mp3"
    assert has_lame_header(dst) is True


@pytest.mark.ffmpeg
@pytest.mark.lame
def test_mp3_320_byte_identical_to_legacy_path(tmp_path):
    """The lame-fragment refactor must not change the encoded bytes: the mp3-320
    profile's encoder output equals the historical `_lame_pipe_convert(.., 320)`.
    Compared at the raw-pipe level (no tag apply) so only audio bytes matter."""
    from convert_lossless import _lame_pipe, _lame_pipe_convert
    src = tmp_path / "a.flac"
    make_flac(src)
    new = tmp_path / "new.mp3"
    old = tmp_path / "old.mp3"
    assert _lame_pipe(src, new, PROFILES["mp3-320"].lame_args) is True
    assert _lame_pipe_convert(src, old, 320) is True
    assert new.read_bytes() == old.read_bytes()


# ── Opus profiles ─────────────────────────────────────────────────────────────

@pytest.mark.opus
@pytest.mark.parametrize("pid", ["opus-192", "opus-160", "opus-128", "opus-96", "opus-64"])
def test_opus_profiles_encode_valid(tmp_path, pid):
    from convert_lossless import _valid_opus, convert_audio
    src = tmp_path / "a.flac"
    make_flac(src, title="Song", artist="Band")
    dst = tmp_path / f"{pid}.opus"
    assert convert_audio(src, dst, PROFILES[pid]) is True
    assert _codec(dst) == "opus"
    assert _valid_opus(dst) is True


def test_opus_command_pins_quality_knobs(monkeypatch):
    """The opus encode always pins max complexity + unconstrained VBR, and uses
    the soxr resampler when (and only when) the ffmpeg build has libsoxr."""
    import convert_lossless as cl

    captured = {}

    class _R:
        returncode = 0
    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R()
    monkeypatch.setattr(cl.subprocess, "run", _fake_run)
    cl._has_soxr.cache_clear()

    monkeypatch.setattr(cl, "_has_soxr", lambda: True)
    cl._opus_convert(cl.Path("in.flac"), cl.Path("out.opus"), 128)
    cmd = captured["cmd"]
    assert "-compression_level" in cmd and cmd[cmd.index("-compression_level") + 1] == "10"
    assert cmd[cmd.index("-vbr") + 1] == "on"
    assert "-af" in cmd and "resampler=soxr" in cmd[cmd.index("-af") + 1]

    monkeypatch.setattr(cl, "_has_soxr", lambda: False)
    cl._opus_convert(cl.Path("in.flac"), cl.Path("out.opus"), 128)
    cmd = captured["cmd"]
    assert "-compression_level" in cmd            # complexity still pinned
    assert "-af" not in cmd                        # no soxr → default resampler


@pytest.mark.opus
def test_opus_64_preserves_mono(tmp_path):
    from convert_lossless import convert_audio
    src = tmp_path / "mono.flac"
    _mono_flac(src)
    dst = tmp_path / "mono.opus"
    assert convert_audio(src, dst, PROFILES["opus-64"]) is True
    assert _channels(dst) == 1


@pytest.mark.opus
def test_opus_output_gets_tags_via_tagio(tmp_path):
    import tagio
    from convert_lossless import convert_audio
    src = tmp_path / "a.flac"
    make_flac(src, title="Song One", artist="The Band", album="Record",
              albumartist="The Band", date="2019", genre="Rock", tracknumber="1")
    dst = tmp_path / "out.opus"
    assert convert_audio(src, dst, PROFILES["opus-128"]) is True
    tags = tagio.open_audio(dst).read()
    assert tags["title"] == "Song One"
    assert tags["artist"] == "The Band"
    assert tags["album"] == "Record"
    assert tags["album_artist"] == "The Band"
    assert tags["date"] == "2019"
    assert tags["genre"] == "Rock"
