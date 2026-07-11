"""
Tests for the Collections feature in browse.py.

The pure mutators (create/rename/delete/remove) operate on a plain cfg dict and
need no library. Resolving a collection to album rows (all_collections /
collection_albums / add_to_collection) reads real MP3s, so those carry the
ffmpeg marker via library_factory.
"""
import pytest

import browse


# ── Pure cfg mutators (no ffmpeg) ─────────────────────────────────────────────

def test_create_collection():
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    assert [c["name"] for c in cfg["collections"]] == ["Faves"]
    assert cfg["collections"][0]["albums"] == []


def test_create_trims_name():
    cfg = {"collections": []}
    browse.create_collection(cfg, "  Road Trip  ")
    assert cfg["collections"][0]["name"] == "Road Trip"


def test_create_duplicate_is_case_insensitive():
    cfg = {"collections": [{"name": "Faves", "albums": []}]}
    with pytest.raises(ValueError):
        browse.create_collection(cfg, "faves")


def test_create_blank_raises():
    with pytest.raises(ValueError):
        browse.create_collection({"collections": []}, "   ")


def test_rename_collection():
    cfg = {"collections": [{"name": "Faves", "albums": []}]}
    browse.rename_collection(cfg, "Faves", "Best Of")
    assert cfg["collections"][0]["name"] == "Best Of"


def test_rename_to_same_name_case_change_is_allowed():
    cfg = {"collections": [{"name": "faves", "albums": []}]}
    browse.rename_collection(cfg, "faves", "Faves")   # clash is the same collection
    assert cfg["collections"][0]["name"] == "Faves"


def test_rename_missing_raises():
    with pytest.raises(ValueError):
        browse.rename_collection({"collections": []}, "Nope", "New")


def test_rename_clash_raises():
    cfg = {"collections": [{"name": "A", "albums": []}, {"name": "B", "albums": []}]}
    with pytest.raises(ValueError):
        browse.rename_collection(cfg, "A", "b")


def test_delete_collection():
    cfg = {"collections": [{"name": "A", "albums": []}, {"name": "B", "albums": []}]}
    browse.delete_collection(cfg, "A")
    assert [c["name"] for c in cfg["collections"]] == ["B"]


def test_delete_missing_raises():
    with pytest.raises(ValueError):
        browse.delete_collection({"collections": []}, "Nope")


def test_remove_from_collection_by_path(tmp_path):
    cfg = {"collections": [{"name": "A", "albums": [
        {"path": "Artist/2020 - X", "artist": "Artist", "album": "X", "year": "2020"},
    ]}]}
    browse.remove_from_collection(tmp_path, cfg, "A", str(tmp_path / "Artist/2020 - X"))
    assert cfg["collections"][0]["albums"] == []


def test_remove_missing_album_is_noop(tmp_path):
    cfg = {"collections": [{"name": "A", "albums": [
        {"path": "Artist/2020 - X", "artist": "Artist", "album": "X", "year": "2020"},
    ]}]}
    browse.remove_from_collection(tmp_path, cfg, "A", str(tmp_path / "Artist/2020 - Y"))
    assert len(cfg["collections"][0]["albums"]) == 1


def test_remove_from_missing_collection_raises(tmp_path):
    with pytest.raises(ValueError):
        browse.remove_from_collection(tmp_path, {"collections": []}, "Nope", "x")


# ── Resolving refs to album rows (needs a real library) ───────────────────────

pytestmark_ffmpeg = pytest.mark.ffmpeg


@pytest.fixture
def lib(library_factory):
    return library_factory({
        "AC-DC/1980 - Back in Black":      [{"TIT2": "Hells Bells"}],
        "Queen/1975 - A Night at the Opera": [{"TIT2": "Bohemian Rhapsody"}],
    })


@pytest.mark.ffmpeg
def test_add_and_browse_collection(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    album = lib / "AC-DC/1980 - Back in Black"
    browse.add_to_collection(lib, cfg, "Faves", album)

    ref = cfg["collections"][0]["albums"][0]
    assert ref["path"] == "AC-DC/1980 - Back in Black"      # stored relative
    assert ref["album"] == "Back in Black" and ref["artist"] == "AC-DC"

    rows, changed = browse.collection_albums(lib, cfg, "Faves")
    assert not changed
    assert [r["album"] for r in rows] == ["Back in Black"]
    assert rows[0]["album_path"] == str(album)             # absolute in the row


@pytest.mark.ffmpeg
def test_add_is_idempotent(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    album = lib / "AC-DC/1980 - Back in Black"
    browse.add_to_collection(lib, cfg, "Faves", album)
    browse.add_to_collection(lib, cfg, "Faves", album)
    assert len(cfg["collections"][0]["albums"]) == 1


@pytest.mark.ffmpeg
def test_add_unknown_album_raises(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    with pytest.raises(ValueError):
        browse.add_to_collection(lib, cfg, "Faves", lib / "Nope/2000 - Ghost")


@pytest.mark.ffmpeg
def test_all_collections_counts_only_resolvable(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    browse.add_to_collection(lib, cfg, "Faves", lib / "AC-DC/1980 - Back in Black")
    # A stale ref that resolves to nothing (bad path + non-matching metadata).
    cfg["collections"][0]["albums"].append(
        {"path": "Ghost/2000 - Nope", "artist": "Ghost", "album": "Nope", "year": "2000"})

    colls = browse.all_collections(lib, cfg)
    assert colls == [{"name": "Faves", "count": 1}]        # stale ref not counted


@pytest.mark.ffmpeg
def test_all_collections_sorted_by_name(lib):
    cfg = {"collections": []}
    for name in ("Zeta", "alpha", "Mid"):
        browse.create_collection(cfg, name)
    assert [c["name"] for c in browse.all_collections(lib, cfg)] == ["alpha", "Mid", "Zeta"]


@pytest.mark.ffmpeg
def test_collection_preserves_insertion_order(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Mix")
    browse.add_to_collection(lib, cfg, "Mix", lib / "Queen/1975 - A Night at the Opera")
    browse.add_to_collection(lib, cfg, "Mix", lib / "AC-DC/1980 - Back in Black")
    rows, _ = browse.collection_albums(lib, cfg, "Mix")
    # Stored order (Queen first), not A-Z — the grid re-sorts client-side.
    assert [r["album"] for r in rows] == ["A Night at the Opera", "Back in Black"]


@pytest.mark.ffmpeg
def test_collection_self_heals_renamed_folder(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    album = lib / "Queen/1975 - A Night at the Opera"
    browse.add_to_collection(lib, cfg, "Faves", album)

    # Simulate standardize renaming the album folder (tags unchanged).
    renamed = lib / "Queen/1975 - A Night At The Opera [Remastered]"
    album.rename(renamed)

    rows, changed = browse.collection_albums(lib, cfg, "Faves")
    assert changed                                          # a heal happened
    assert len(rows) == 1 and rows[0]["album_path"] == str(renamed)
    # The stored ref was rewritten to the new path so the next read is a hit.
    assert cfg["collections"][0]["albums"][0]["path"] == \
        "Queen/1975 - A Night At The Opera [Remastered]"


@pytest.mark.ffmpeg
def test_collection_drops_unresolvable_ref(lib):
    cfg = {"collections": []}
    browse.create_collection(cfg, "Faves")
    browse.add_to_collection(lib, cfg, "Faves", lib / "AC-DC/1980 - Back in Black")
    cfg["collections"][0]["albums"].insert(
        0, {"path": "Gone/1999 - Vanished", "artist": "Gone", "album": "Vanished", "year": "1999"})

    rows, _ = browse.collection_albums(lib, cfg, "Faves")
    assert [r["album"] for r in rows] == ["Back in Black"]   # missing one skipped


@pytest.mark.ffmpeg
def test_missing_collection_returns_empty(lib):
    rows, changed = browse.collection_albums(lib, {"collections": []}, "Nope")
    assert rows == [] and changed is False
