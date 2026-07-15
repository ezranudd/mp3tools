"""
tagio backend tests.

Phase 1: the MP3/ID3 backend must reproduce the tools' historical helpers
exactly — the golden comparisons below pin `Mp3Tags` / `mp3_read_framekey`
against `audit`'s originals so the migration can't drift MP3 behaviour.
"""
import pytest
from mutagen.id3 import ID3, TXXX

import audit
import tagio

from conftest import (TINY_PNG, add_id3v1, embed_art, make_mp3, make_v24)

pytestmark = pytest.mark.ffmpeg


@pytest.fixture()
def mp3(tmp_path):
    p = tmp_path / "01. Test Artist - Silent Night.mp3"
    make_mp3(p)
    return p


# ── Legacy frame-key contract: identical to audit.read_tags ──────────────────

def test_framekey_matches_audit_read_tags(mp3):
    assert tagio.mp3_read_framekey(mp3) == audit.read_tags(mp3)


def test_framekey_headerless(tmp_path):
    p = tmp_path / "bare.mp3"
    make_mp3(p)
    ID3(p, translate=False).delete(p)
    assert tagio.mp3_read_framekey(p) == audit.read_tags(p)
    t = tagio.mp3_read_framekey(p)
    assert t is not None and t["_version"] is None and t["TIT2"] is None


def test_framekey_v24(mp3):
    make_v24(mp3)
    assert tagio.mp3_read_framekey(mp3) == audit.read_tags(mp3)
    assert tagio.mp3_read_framekey(mp3)["_version"][:2] == (2, 4)


# ── Leaf helpers identical to audit's ────────────────────────────────────────

def test_leaf_helpers_match_audit(mp3):
    tags = ID3(mp3, translate=False)
    assert tagio.album_artist_value(tags) == audit.album_artist_value(tags)
    assert tagio.has_id3v1(mp3) == audit._has_id3v1(mp3)
    assert tagio.has_embedded_art(mp3) == audit.has_embedded_art(mp3)
    add_id3v1(mp3)
    assert tagio.has_id3v1(mp3) is True
    embed_art(mp3)
    assert tagio.has_embedded_art(mp3) is True


# ── Canonical model ──────────────────────────────────────────────────────────

def test_canonical_read(mp3):
    c = tagio.open_audio(mp3).read()
    assert set(c) == set(tagio.CANONICAL_KEYS)
    assert c["title"] == "Silent Night"
    assert c["artist"] == "Test Artist"
    assert c["album_artist"] == "Test Artist"
    assert c["album"] == "Test Album"
    assert c["date"] == "2024"
    assert c["track"] == "01/1"
    assert c["disc"] is None


def test_canonical_write_roundtrip(mp3):
    a = tagio.open_audio(mp3)
    a.write({"title": "New Title", "artist": "New Artist",
             "album_artist": "New AA", "album": "New Album",
             "date": "1999", "genre": "Jazz", "track": "03/12", "disc": "1/2"})
    c = tagio.open_audio(mp3).read()
    assert c["title"] == "New Title"
    assert c["album_artist"] == "New AA"
    assert c["date"] == "1999"
    assert c["track"] == "03/12"
    assert c["disc"] == "1/2"
    # Canonical album_artist writes TPE2 (v2.3-native), no legacy frames.
    raw = ID3(mp3, translate=False)
    assert str(raw["TPE2"]) == "New AA"
    assert not [k for k in raw if k.startswith("TXXX")]
    # Untouched keys stay put: a partial write changes only given fields.
    a2 = tagio.open_audio(mp3)
    a2.write({"genre": "Rock"})
    assert tagio.open_audio(mp3).read()["title"] == "New Title"


def test_canonical_album_artist_legacy_fallback(mp3):
    raw = ID3(mp3, translate=False)
    raw.delall("TPE2")
    raw.add(TXXX(encoding=3, desc="ALBUM ARTIST", text="Legacy AA"))
    raw.save(mp3, v2_version=3, v1=0)
    assert tagio.open_audio(mp3).read()["album_artist"] == "Legacy AA"
    assert tagio.open_audio(mp3).diagnostics()["legacy_albumartist"] is True


# ── Cover art ────────────────────────────────────────────────────────────────

def test_cover_roundtrip(mp3):
    a = tagio.open_audio(mp3)
    assert a.has_cover() is False and a.get_cover() is None
    a.set_cover(TINY_PNG, "image/png")
    a = tagio.open_audio(mp3)
    assert a.has_cover() is True
    data, mime = a.get_cover()
    assert data == TINY_PNG and mime == "image/png"
    a.remove_cover()
    assert tagio.open_audio(mp3).has_cover() is False


# ── info + diagnostics ───────────────────────────────────────────────────────

def test_info(mp3):
    i = tagio.open_audio(mp3).info()
    assert i["bitrate_kbps"] and i["bitrate_kbps"] > 0
    assert i["length_sec"] and i["length_sec"] > 0


def test_diagnostics(mp3):
    d = tagio.open_audio(mp3).diagnostics()
    assert d["id3_version"][:2] == (2, 3)
    assert d["has_id3v1"] is False
    assert d["legacy_albumartist"] is False
    assert d["tyer"] == "2024" and d["tdrc"] is None
    make_v24(mp3)
    d = tagio.open_audio(mp3).diagnostics()
    assert d["id3_version"][:2] == (2, 4)
    assert d["tdrc"] == "2024" and d["tyer"] is None


# ── Dispatch ─────────────────────────────────────────────────────────────────

def test_open_audio_dispatch(mp3, tmp_path):
    assert isinstance(tagio.open_audio(mp3), tagio.Mp3Tags)
    assert tagio.open_audio(tmp_path / "cover.jpg") is None      # unknown ext
    bad = tmp_path / "corrupt.mp3"
    bad.write_bytes(b"not an mp3" * 50)
    # Corrupt but present → None only if the header parse hard-fails; a garbage
    # file with no ID3 header opens as headerless (all-None read), matching
    # mp3_read_framekey. Assert both stay consistent.
    assert (tagio.open_audio(bad) is None) == (tagio.mp3_read_framekey(bad) is None)
