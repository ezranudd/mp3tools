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
