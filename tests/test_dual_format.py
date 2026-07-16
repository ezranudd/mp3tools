"""
Both-format (mp3 + opus) audit/standardize behavior.

Portable checks are parametrized over the two formats via make_audio; the opus
parameter carries @pytest.mark.opus so it auto-skips without ffmpeg libopus.
Format-specific behavior (ID3 version/v1 enforcement) stays in the MP3 suites.
"""
import pytest
from mutagen.id3 import ID3

import audit
import standardize as st
import tagio

from conftest import TINY_PNG, embed_art, make_audio

pytestmark = pytest.mark.ffmpeg

# Both formats; opus auto-skips when libopus is missing.
FORMATS = [("mp3", ".mp3"),
           pytest.param("opus", ".opus", marks=pytest.mark.opus)]


def _clean_album(root, fmt, ext, n=2):
    """A fully compliant album of *fmt*, cover.jpg + embedded art (both mode)."""
    folder = root / "The Band" / "2019 - Record"
    folder.mkdir(parents=True)
    for i in range(1, n + 1):
        p = folder / f"{i:02d}. The Band - Song {i}{ext}"
        make_audio(fmt, p, title=f"Song {i}", artist="The Band",
                   album_artist="The Band", album="Record", date="2019",
                   genre="Rock", track=f"{i:02d}/{n}")
        if fmt == "mp3":
            embed_art(p)
        else:
            tagio.open_audio(p).set_cover(TINY_PNG, "image/png")
    (folder / "cover.jpg").write_bytes(TINY_PNG)
    return folder


def _cats(root):
    cats = set()
    for _f, ai, files in audit.scan(root):
        cats |= {i.cat for i in ai}
        for _p, _t, iss in files:
            cats |= {i.cat for i in iss}
    return cats


# ── Audit: portable checks fire identically in both formats ──────────────────

@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_clean_album_zero_issues(tmp_path, fmt, ext):
    _clean_album(tmp_path, fmt, ext)
    assert _cats(tmp_path) == set()


@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_unpadded_track_flagged(tmp_path, fmt, ext):
    folder = _clean_album(tmp_path, fmt, ext)
    f = folder / f"01. The Band - Song 1{ext}"
    tagio.open_audio(f).write({"track": "1/2"})     # unpadded
    assert "TRACK_PAD" in _cats(tmp_path)


@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_unnormalized_date_flagged(tmp_path, fmt, ext):
    folder = _clean_album(tmp_path, fmt, ext)
    for p in folder.glob(f"*{ext}"):
        tagio.open_audio(p).write({"date": "2019-05-01"})
    assert "DATE_NORM" in _cats(tmp_path)


@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_wrong_filename_flagged(tmp_path, fmt, ext):
    folder = _clean_album(tmp_path, fmt, ext)
    (folder / f"01. The Band - Song 1{ext}").rename(folder / f"whatever{ext}")
    assert "FILENAME" in _cats(tmp_path)


# ── Standardize: portable steps work in both formats ─────────────────────────

@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_normalize_year_both_formats(tmp_path, fmt, ext):
    folder = _clean_album(tmp_path, fmt, ext)
    for p in folder.glob(f"*{ext}"):
        tagio.open_audio(p).write({"date": "2019-05-01"})
    st.step_normalize_year(tmp_path, dry_run=False)
    for p in folder.glob(f"*{ext}"):
        assert tagio.open_audio(p).read()["date"] == "2019"


@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_pad_and_total_both_formats(tmp_path, fmt, ext):
    folder = _clean_album(tmp_path, fmt, ext)
    for p in folder.glob(f"*{ext}"):
        tagio.open_audio(p).write({"track": tagio.open_audio(p).read()["track"].split("/")[0]})
    st.step_pad_tracks(tmp_path, dry_run=False)
    st.step_set_total_tracks(tmp_path, dry_run=False)
    tracks = sorted(tagio.open_audio(p).read()["track"] for p in folder.glob(f"*{ext}"))
    assert tracks == ["01/2", "02/2"]


@pytest.mark.parametrize("fmt,ext", FORMATS)
def test_rename_files_both_formats(tmp_path, fmt, ext):
    folder = _clean_album(tmp_path, fmt, ext)
    (folder / f"01. The Band - Song 1{ext}").rename(folder / f"junk{ext}")
    st.step_rename_files(tmp_path, dry_run=False)
    names = sorted(p.name for p in folder.glob(f"*{ext}"))
    assert names == [f"01. The Band - Song 1{ext}", f"02. The Band - Song 2{ext}"]


# ── Format-specific: ID3v2.3 enforcement no-ops on Opus ──────────────────────

@pytest.mark.opus
def test_enforce_id3v23_skips_opus(tmp_path):
    folder = _clean_album(tmp_path, "opus", ".opus")
    # A clean no-op on Opus: zero fixes, no error, tags untouched.
    stats = st.step_enforce_id3v23(tmp_path, dry_run=False)
    assert stats["fixed"] == 0
    first = folder / "01. The Band - Song 1.opus"
    assert tagio.open_audio(first).read()["title"] == "Song 1"


# ── Mixed-format album policy (default allowed; opt-in info flag) ─────────────

@pytest.mark.opus
def test_mixed_format_album_allowed_by_default(tmp_path):
    folder = _clean_album(tmp_path, "mp3", ".mp3", n=1)
    # add an opus track alongside the mp3
    p = folder / "02. The Band - Song 2.opus"
    make_audio("opus", p, title="Song 2", artist="The Band", album_artist="The Band",
               album="Record", date="2019", genre="Rock", track="02/2")
    tagio.open_audio(p).set_cover(TINY_PNG, "image/png")
    # fix the mp3's total to 2 so no TRACK_PAD noise
    m = folder / "01. The Band - Song 1.mp3"
    tagio.open_audio(m).write({"track": "01/2"})
    assert "MIXED_FORMAT" not in _cats(tmp_path)          # default: allowed


@pytest.mark.opus
def test_mixed_format_flagged_when_enabled(tmp_path):
    import settings as settings_mod
    folder = _clean_album(tmp_path, "mp3", ".mp3", n=1)
    p = folder / "02. The Band - Song 2.opus"
    make_audio("opus", p, title="Song 2", artist="The Band", album_artist="The Band",
               album="Record", date="2019", genre="Rock", track="02/2")
    tagio.open_audio(p).set_cover(TINY_PNG, "image/png")
    settings_mod.save(tmp_path, {**settings_mod.load(tmp_path),
                                 "flag_mixed_format_albums": True})
    assert "MIXED_FORMAT" in _cats(tmp_path)
