"""
Full lossless-album import under each encoding-profile family: the output lands
with the profile's extension, correct tags (read back through tagio), and an
embedded cover. Opus cases carry @pytest.mark.opus and auto-skip without libopus.
"""
import pytest

import tagio
from conftest import TINY_PNG, make_flac

pytestmark = pytest.mark.ffmpeg


def _src_album(tmp_path):
    src = tmp_path / "src" / "2019 - Record"
    src.mkdir(parents=True)
    for i in (1, 2):
        make_flac(src / f"{i:02d}. The Band - Song {i}.flac",
                  title=f"Song {i}", artist="The Band", albumartist="The Band",
                  album="Record", date="2019", genre="Rock", tracknumber=str(i))
    (src / "cover.jpg").write_bytes(TINY_PNG)
    return src


@pytest.mark.parametrize("profile,ext", [
    pytest.param("opus-128", ".opus", marks=pytest.mark.opus),
    ("mp3-v0", ".mp3"),
    ("mp3-320", ".mp3"),
])
def test_import_lossless_under_profile(tmp_path, profile, ext):
    from import_tracks import import_tracks
    src = _src_album(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    import_tracks(src, lib, dry_run=False, cover_art="both",
                  settings={"import_profile": profile})

    out = sorted(lib.rglob(f"*{ext}"))
    assert [p.name for p in out] == [f"01. The Band - Song 1{ext}",
                                     f"02. The Band - Song 2{ext}"]
    # No stragglers in the other format.
    other = ".mp3" if ext == ".opus" else ".opus"
    assert not list(lib.rglob(f"*{other}"))

    a = tagio.open_audio(out[0])
    tags = a.read()
    assert tags["title"] == "Song 1"
    assert tags["artist"] == "The Band"
    assert tags["album"] == "Record"
    assert tags["album_artist"] == "The Band"
    assert tags["date"] == "2019"
    assert tags["genre"] == "Rock"
    assert tags["track"] == "01/2"
    assert a.has_cover()
