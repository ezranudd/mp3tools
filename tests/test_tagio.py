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


# ── Opus / Vorbis backend (phase 3) ──────────────────────────────────────────

opusmark = pytest.mark.opus


@pytest.fixture()
def opus(tmp_path):
    from conftest import make_opus
    p = tmp_path / "03. Band - Song.opus"
    make_opus(p, title="Song", artist="Band", albumartist="The Band",
              album="Record", date="2019", genre="Rock",
              tracknumber="3", tracktotal="12", discnumber="1", disctotal="2")
    return p


@opusmark
def test_opus_dispatch_and_format(opus):
    a = tagio.open_audio(opus)
    assert isinstance(a, tagio.OpusTags)
    assert a.format == "opus"
    assert a.diagnostics() == {}          # no ID3 keys → MP3-only checks vanish


@opusmark
def test_opus_read_composes_track_disc(opus):
    c = tagio.open_audio(opus).read()
    assert c["title"] == "Song"
    assert c["artist"] == "Band"
    assert c["album_artist"] == "The Band"
    assert c["album"] == "Record"
    assert c["date"] == "2019"
    assert c["genre"] == "Rock"
    assert c["track"] == "3/12"           # composed from TRACKNUMBER + TRACKTOTAL
    assert c["disc"] == "1/2"


@opusmark
def test_opus_read_totaltracks_fallback(tmp_path):
    from conftest import make_opus
    p = tmp_path / "x.opus"
    make_opus(p, tracknumber="5", totaltracks="9")   # alternate total spelling
    assert tagio.open_audio(p).read()["track"] == "5/9"


@opusmark
def test_opus_read_number_only(tmp_path):
    from conftest import make_opus
    p = tmp_path / "x.opus"
    make_opus(p, tracknumber="7")
    assert tagio.open_audio(p).read()["track"] == "7"


@opusmark
def test_opus_write_roundtrip_and_split(opus):
    from mutagen.oggopus import OggOpus
    a = tagio.open_audio(opus)
    a.write({"title": "New", "album_artist": "New AA", "date": "2001",
             "track": "4/20", "disc": "2/2", "genre": "Jazz"})
    c = tagio.open_audio(opus).read()
    assert c["title"] == "New" and c["album_artist"] == "New AA"
    assert c["date"] == "2001" and c["track"] == "4/20" and c["disc"] == "2/2"
    # Track split into the Vorbis convention; no duplicate total spelling.
    raw = OggOpus(str(opus))
    assert raw["TRACKNUMBER"] == ["4"] and raw["TRACKTOTAL"] == ["20"]
    assert "TOTALTRACKS" not in raw
    # Partial write leaves other fields intact.
    tagio.open_audio(opus).write({"genre": "Funk"})
    assert tagio.open_audio(opus).read()["title"] == "New"


@opusmark
def test_opus_write_track_number_only_clears_total(opus):
    a = tagio.open_audio(opus)
    a.write({"track": "6"})               # was 3/12 → now just 6
    assert tagio.open_audio(opus).read()["track"] == "6"


@opusmark
def test_opus_cover_roundtrip(opus):
    a = tagio.open_audio(opus)
    assert a.has_cover() is False and a.get_cover() is None
    a.set_cover(TINY_PNG, "image/png")
    a = tagio.open_audio(opus)
    assert a.has_cover() is True
    data, mime = a.get_cover()
    assert data == TINY_PNG and mime == "image/png"
    a.remove_cover()
    assert tagio.open_audio(opus).has_cover() is False


@opusmark
def test_opus_info(opus):
    i = tagio.open_audio(opus).info()
    assert i["length_sec"] and i["length_sec"] > 0


@opusmark
def test_opus_tagless_reads_all_none(tmp_path):
    from conftest import make_opus
    p = tmp_path / "bare.opus"
    make_opus(p)
    c = tagio.open_audio(p).read()
    assert all(v is None for v in c.values())
