"""
Library settings — stored in {library_root}/.mp3tools as JSON.
"""
import json
from pathlib import Path

SETTINGS_FILENAME = ".mp3tools"

DEFAULTS: dict = {
    "cover_art":            "folder",  # "folder" | "embed" | "both"
    "cover_art_embed_size": 500,       # pixels; 0 = no resize
}

_VALID_COVER_ART = frozenset(("folder", "embed", "both"))


def load(library_root: Path) -> dict:
    settings = dict(DEFAULTS)
    path = library_root / SETTINGS_FILENAME
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("cover_art"), str) and data["cover_art"] in _VALID_COVER_ART:
                settings["cover_art"] = data["cover_art"]
            if isinstance(data.get("cover_art_embed_size"), int):
                settings["cover_art_embed_size"] = max(0, data["cover_art_embed_size"])
        except Exception:
            pass
    return settings


def save(library_root: Path, settings: dict) -> None:
    path = library_root / SETTINGS_FILENAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: settings[k] for k in DEFAULTS if k in settings}, f, indent=2)
