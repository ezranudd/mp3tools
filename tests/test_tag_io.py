"""
Tag I/O tests (real MP3s, so ffmpeg is required throughout).

Covers the per-module read/write contracts: browse read_tags/write_tags,
audit read_tags/_has_id3v1/has_embedded_art, and the album-artist canonical
TPE2 handling (standardize/import_tracks vs browse's divergent copy).
"""
import pytest
from mutagen.id3 import ID3, TIT2, TXXX

import audit
import browse
import import_tracks
import standardize

from conftest import TINY_PNG, add_id3v1, embed_art, make_mp3, make_v24

pytestmark = pytest.mark.ffmpeg


@pytest.fixture
def mp3(tmp_path):
    p = tmp_path / "01. Test Artist - Silent Night.mp3"
    make_mp3(p)
    return p


# ── browse.read_tags ──────────────────────────────────────────────────────────

def test_browse_read_tags_full(mp3):
    t = browse.read_tags(mp3)
    assert t["title"] == "Silent Night"
    assert t["artist"] == "Test Artist"
    assert t["albumartist"] == "Test Artist"
    assert t["album"] == "Test Album"
    assert t["year"] == "2024"
    assert t["track"] == "01/1"
    assert t["bitrate"] and t["length"] and t["length_sec"] >= 0


def test_browse_read_tags_missing_frames(tmp_path):
    p = tmp_path / "sparse.mp3"
    make_mp3(p, TIT2="Only Title")
    t = browse.read_tags(p)
    assert t["title"] == "Only Title"
    assert t["artist"] == "" and t["album"] == "" and t["year"] == ""


def test_browse_read_tags_v24_year_via_tdrc(mp3):
    make_v24(mp3)
    t = browse.read_tags(mp3)
    assert t["year"] == "2024"          # TYER gone, TDRC fallback used


def test_browse_read_tags_corrupt_returns_empty(tmp_path):
    p = tmp_path / "corrupt.mp3"
    p.write_bytes(b"\x00" * 64)
    assert browse.read_tags(p) == {}


def test_browse_read_tags_truncated_mp3(mp3):
    # Chop the file mid-audio: tags (at the front) may or may not survive,
    # but the reader must return a dict, not raise.
    data = mp3.read_bytes()
    mp3.write_bytes(data[: len(data) // 2])
    t = browse.read_tags(mp3)
    assert isinstance(t, dict)


# ── browse.write_tags round-trip ──────────────────────────────────────────────

def test_browse_write_read_roundtrip_unicode(mp3):
    browse.write_tags(mp3, {"TIT2": "Naïve — Résumé", "TPE1": "Björk",
                            "TCON": "Jazz", "TYER": "1999"})
    t = browse.read_tags(mp3)
    assert t["title"] == "Naïve — Résumé"
    assert t["artist"] == "Björk"
    assert t["genre"] == "Jazz"
    assert t["year"] == "1999"


def test_browse_write_keeps_id3v23_no_v1(mp3):
    browse.write_tags(mp3, {"TIT2": "X"})
    tags = ID3(mp3, translate=False)
    assert tags.version[:2] == (2, 3)
    assert not audit._has_id3v1(mp3)


def test_browse_write_unknown_frame_ignored(mp3):
    before = browse.read_tags(mp3)
    browse.write_tags(mp3, {"BOGUS": "x"})
    assert browse.read_tags(mp3) == before


# ── audit.read_tags contract ──────────────────────────────────────────────────

def test_audit_read_tags_full(mp3):
    t = audit.read_tags(mp3)
    assert t["TIT2"] == "Silent Night"
    assert t["ALBUMARTIST"] == "Test Artist"
    assert t["TDRC"] is None
    assert t["_version"][:2] == (2, 3)


def test_audit_read_tags_no_header(tmp_path):
    # A tagless (but real) MP3 → all-None dict, not None.
    p = tmp_path / "bare.mp3"
    make_mp3(p)
    ID3(p, translate=False).delete(p)
    t = audit.read_tags(p)
    assert t is not None
    assert t["TIT2"] is None and t["ALBUMARTIST"] is None and t["_version"] is None


def test_audit_read_tags_v24(mp3):
    make_v24(mp3)
    t = audit.read_tags(mp3)
    assert t["_version"][:2] == (2, 4)
    assert t["TYER"] is None and t["TDRC"] == "2024"


# ── audit._has_id3v1 / has_embedded_art ───────────────────────────────────────

def test_has_id3v1(mp3):
    assert not audit._has_id3v1(mp3)
    add_id3v1(mp3)
    assert audit._has_id3v1(mp3)
    assert not audit._has_id3v1(mp3.parent / "absent.mp3")


def test_has_embedded_art(mp3):
    assert not audit.has_embedded_art(mp3)
    embed_art(mp3)
    assert audit.has_embedded_art(mp3)
    assert not audit.has_embedded_art(mp3.parent / "absent.mp3")


# ── Album-artist canonical handling ───────────────────────────────────────────

LEGACY_TXXX_DESCS = ["album artist", "ALBUMARTIST", "ALBUM ARTIST",
                     "AlbumArtist", "Album Artist"]


def _plant_legacy_albumartist(path, desc, value):
    tags = ID3(path, translate=False)
    tags.delall("TPE2")
    tags.add(TXXX(encoding=3, desc=desc, text=value))
    tags.save(path, v2_version=3, v1=0)


@pytest.mark.parametrize("mod", [standardize, import_tracks, audit],
                         ids=lambda m: m.__name__)
@pytest.mark.parametrize("desc", LEGACY_TXXX_DESCS)
def test_album_artist_value_reads_legacy_txxx(mp3, mod, desc):
    _plant_legacy_albumartist(mp3, desc, "Legacy Artist")
    tags = ID3(mp3, translate=False)
    assert mod.album_artist_value(tags) == "Legacy Artist"


@pytest.mark.parametrize("mod", [standardize, import_tracks],
                         ids=lambda m: m.__name__)
def test_set_album_artist_migrates_to_tpe2_only(mp3, mod, tmp_path):
    _plant_legacy_albumartist(mp3, "ALBUM ARTIST", "Legacy Artist")
    tags = ID3(mp3, translate=False)
    mod.set_album_artist(tags, "Canonical Artist")
    tags.save(mp3, v2_version=3, v1=0)

    tags = ID3(mp3, translate=False)
    assert str(tags["TPE2"]) == "Canonical Artist"
    assert not [k for k in tags if k.startswith("TXXX")], \
        "legacy TXXX album-artist variants must be deleted"


def test_standardize_album_artist_prefers_tpe2(mp3):
    # TPE2 is first in ALBUM_ARTIST_KEYS for standardize/import/audit.
    tags = ID3(mp3, translate=False)
    tags.add(TXXX(encoding=3, desc="ALBUM ARTIST", text="Legacy"))
    assert standardize.album_artist_value(tags) == "Test Artist"


def test_browse_album_artist_prefers_tpe2(mp3):
    # TPE2 is first in browse._ALBUM_ARTIST_KEYS, same as the other modules —
    # a stale legacy frame must not shadow the canonical value.
    tags = ID3(mp3, translate=False)
    tags.add(TXXX(encoding=3, desc="ALBUM ARTIST", text="Legacy"))
    assert browse._album_artist_value(tags) == "Test Artist"


def test_browse_album_artist_legacy_fallback(mp3):
    # With TPE2 absent, the legacy TXXX variants are still read (migration).
    _plant_legacy_albumartist(mp3, "ALBUM ARTIST", "Legacy Artist")
    tags = ID3(mp3, translate=False)
    assert browse._album_artist_value(tags) == "Legacy Artist"


def test_browse_set_album_artist_writes_tpe2_only(mp3):
    # Starting from a legacy-tagged file, an album-artist edit through browse
    # migrates to TPE2 and deletes every TXXX variant — never writes one back.
    _plant_legacy_albumartist(mp3, "ALBUM ARTIST", "Legacy Artist")
    browse.write_tags(mp3, {"ALBUMARTIST": "New AA"})
    tags = ID3(mp3, translate=False)
    assert str(tags["TPE2"]) == "New AA"
    assert not [k for k in tags if k.startswith("TXXX")]
