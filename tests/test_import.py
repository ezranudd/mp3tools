"""
import_tracks conversion and tag-transfer tests (ffmpeg), plus the pure
cover/fill helpers (which need no audio, only PIL for the resize paths).
"""
from pathlib import Path

import pytest
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from convert_lossless import _apply_source_tags
from import_tracks import (
    _find_cover,
    _prepare_cover_data,
    convert_to_mp3_progress,
    fill_album_tags,
    fill_track_tags,
)

from conftest import TINY_PNG, make_flac, make_m4a


# ── convert_to_mp3_progress (ffmpeg) ─────────────────────────────────────────

@pytest.mark.ffmpeg
@pytest.mark.parametrize("maker,ext", [(make_flac, ".flac"), (make_m4a, ".m4a")])
def test_convert_to_mp3_progress(tmp_path, maker, ext):
    src = tmp_path / f"track{ext}"
    maker(src)
    dst = tmp_path / "track.mp3"
    events = []

    def progress(name, pct, done=False):
        events.append((name, pct, done))

    ok = convert_to_mp3_progress(src, dst, 192, progress=progress)

    assert ok is True
    info = MP3(dst).info                    # a real decodable MP3 came out
    assert 0.5 < info.length < 2.0
    assert events, "progress callback must be invoked"
    assert events[0][0] == src.name
    assert events[-1] == (src.name, 100, True)


@pytest.mark.ffmpeg
def test_apply_source_tags_carries_flac_tags(tmp_path):
    src = tmp_path / "track.flac"
    make_flac(src, title="Song", artist="Band", albumartist="The Band",
              album="Record", date="2019-06-01", genre="Rock", tracknumber="3/10")
    dst = tmp_path / "track.mp3"
    assert convert_to_mp3_progress(src, dst, 192, progress=lambda *a, **k: None)

    _apply_source_tags(src, dst)

    t = ID3(dst, translate=False)
    assert str(t["TIT2"]) == "Song"
    assert str(t["TPE1"]) == "Band"
    assert str(t["TPE2"]) == "The Band"      # albumartist → TPE2
    assert str(t["TALB"]) == "Record"
    assert str(t["TYER"]) == "2019"          # year extracted from full date
    assert str(t["TCON"]) == "Rock"
    assert str(t["TRCK"]) == "3/10"


# ── fill_album_tags / fill_track_tags (pure) ─────────────────────────────────

@pytest.mark.parametrize("dry_run", [True, False])
def test_fill_album_tags(dry_run):
    group = [
        (Path("a.mp3"), {"TPE1": "Band", "TALB": None, "YEAR": None, "TCON": None}),
        (Path("b.mp3"), {"TPE1": None, "TALB": "Record", "YEAR": None, "TCON": "Rock"}),
    ]
    fill_album_tags(group, "2020 - Record", dry_run)
    a, b = group[0][1], group[1][1]
    assert a["YEAR"] == b["YEAR"] == "2020"   # from the folder label
    assert a["TCON"] == "Unknown" and b["TCON"] == "Rock"
    assert a["TALB"] == "Record"              # propagated from the other track
    assert b["TPE1"] == "Band"
    assert a["ALBUMARTIST"] == b["ALBUMARTIST"] == "Band"


def test_fill_album_tags_year_fallback_1900():
    group = [(Path("a.mp3"), {"YEAR": None})]
    fill_album_tags(group, "No Year Here", False)
    assert group[0][1]["YEAR"] == "1900"


def test_fill_track_tags():
    td = {"TIT2": "Kept"}
    fill_track_tags(Path("01. Artist - Other.mp3"), td, False)
    assert td["TIT2"] == "Kept"                       # existing title wins

    td = {}
    fill_track_tags(Path("01. Artist - My Song.mp3"), td, False)
    assert td["TIT2"] == "My Song"                    # after " - "

    td = {}
    fill_track_tags(Path("07. Numbered Title.mp3"), td, False)
    assert td["TIT2"] == "Numbered Title"             # leading digits stripped

    td = {}
    fill_track_tags(Path("justastem.mp3"), td, False)
    assert td["TIT2"] == "justastem"


# ── _find_cover / _prepare_cover_data ────────────────────────────────────────

def test_find_cover_prefers_cover_stem(tmp_path):
    (tmp_path / "aaa.png").write_bytes(TINY_PNG)
    (tmp_path / "cover.jpg").write_bytes(TINY_PNG)
    assert _find_cover(tmp_path) == tmp_path / "cover.jpg"


def test_find_cover_falls_back_to_any_image(tmp_path):
    (tmp_path / "scan.png").write_bytes(TINY_PNG)
    (tmp_path / "notes.txt").write_text("x")
    assert _find_cover(tmp_path) == tmp_path / "scan.png"


def test_find_cover_none(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    assert _find_cover(tmp_path) is None


def test_prepare_cover_data_small_image_passthrough(tmp_path):
    p = tmp_path / "cover.png"
    p.write_bytes(TINY_PNG)
    data, mime = _prepare_cover_data(p, max_size=500)
    assert data == TINY_PNG and mime == "image/png"


def test_prepare_cover_data_resizes_large_image(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    import io
    p = tmp_path / "cover.png"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "PNG")

    data, mime = _prepare_cover_data(p, max_size=200)

    assert mime == "image/jpeg"               # resize re-encodes as JPEG
    out = Image.open(io.BytesIO(data))
    assert max(out.size) <= 200


def test_prepare_cover_data_unreadable_returns_none(tmp_path):
    assert _prepare_cover_data(tmp_path / "absent.png", 500) is None
