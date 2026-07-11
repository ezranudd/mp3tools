"""
Tests for settings.py (pure JSON I/O scoped to the library root — no ffmpeg).
"""
import json

import settings as settings_mod


def test_settings_migration(tmp_path):
    # Old layout: a .mp3tools JSON file + a sibling background image file.
    (tmp_path / ".mp3tools").write_text(json.dumps({"cover_art": "both"}), encoding="utf-8")
    (tmp_path / ".mp3tools-background").write_bytes(b"oldbg")

    cfg = settings_mod.load(tmp_path)

    # Value preserved through the move.
    assert cfg["cover_art"] == "both"
    # .mp3tools is now a folder holding mp3tools.conf + background.
    assert (tmp_path / ".mp3tools").is_dir()
    assert (tmp_path / ".mp3tools" / "mp3tools.conf").is_file()
    assert (tmp_path / ".mp3tools" / "background").read_bytes() == b"oldbg"
    # Legacy files are gone.
    assert not (tmp_path / ".mp3tools-background").exists()


# ── Collections persistence ───────────────────────────────────────────────────

def test_collections_default_empty(tmp_path):
    assert settings_mod.load(tmp_path)["collections"] == []


def test_collections_roundtrip(tmp_path):
    cfg = settings_mod.load(tmp_path)
    cfg["collections"] = [{"name": "Faves", "albums": [
        {"path": "Artist/2020 - X", "artist": "Artist", "album": "X", "year": "2020"},
    ]}]
    settings_mod.save(tmp_path, cfg)
    back = settings_mod.load(tmp_path)
    assert back["collections"] == cfg["collections"]


def test_collections_malformed_entries_dropped(tmp_path):
    # A grab-bag of junk: non-dict, blank name, duplicate name, and a ref missing
    # its path — all pruned; the good bits survive with the canonical shape.
    raw = {"collections": [
        "not a dict",
        {"albums": []},                                  # no name
        {"name": "   "},                                 # blank name
        {"name": "Good", "albums": [
            {"path": "A/2020 - X", "artist": "A", "album": "X", "year": "2020", "extra": "drop"},
            {"artist": "no path"},                       # ref without a path
            {"path": "A/2020 - X"},                      # duplicate path
        ]},
        {"name": "good", "albums": []},                  # duplicate name (case-insensitive)
    ]}
    (settings_mod.settings_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
    (settings_mod.conf_path(tmp_path)).write_text(json.dumps(raw), encoding="utf-8")

    colls = settings_mod.load(tmp_path)["collections"]
    assert [c["name"] for c in colls] == ["Good"]
    assert colls[0]["albums"] == [
        {"path": "A/2020 - X", "artist": "A", "album": "X", "year": "2020"},
    ]


def test_collections_non_list_becomes_empty(tmp_path):
    (settings_mod.settings_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
    (settings_mod.conf_path(tmp_path)).write_text(
        json.dumps({"collections": {"not": "a list"}}), encoding="utf-8")
    assert settings_mod.load(tmp_path)["collections"] == []
