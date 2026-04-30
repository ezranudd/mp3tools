"""
Library settings — stored in {library_root}/.mp3tools as JSON.
"""
import json
import copy
from pathlib import Path

SETTINGS_FILENAME = ".mp3tools"
ART_SOURCE_ORDER = ["itunes", "musicbrainz", "theaudiodb", "discogs"]

DEFAULTS: dict = {
    "cover_art":            "folder",  # "folder" | "embed" | "both"
    "cover_art_embed_size": 500,       # pixels; 0 = no resize
    "enforce_artist_equals_album_artist": False,
    "fetch_art_online":     False,     # run step 15 during standardize
    "art_sources": {
        "itunes":       True,
        "musicbrainz":  True,
        "theaudiodb":   False,
        "discogs":      False,         # browse only; never used by standardize batch
    },
    "art_source_order": list(ART_SOURCE_ORDER),
    "theaudiodb_api_key": "",
    "discogs_token":      "",
}

_VALID_COVER_ART = frozenset(("folder", "embed", "both"))
_VALID_ART_SOURCES = frozenset(ART_SOURCE_ORDER)


def load(library_root: Path) -> dict:
    settings = copy.deepcopy(DEFAULTS)
    path = library_root / SETTINGS_FILENAME
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("cover_art"), str) and data["cover_art"] in _VALID_COVER_ART:
                settings["cover_art"] = data["cover_art"]
            if isinstance(data.get("cover_art_embed_size"), int):
                settings["cover_art_embed_size"] = max(0, data["cover_art_embed_size"])
            if isinstance(data.get("enforce_artist_equals_album_artist"), bool):
                settings["enforce_artist_equals_album_artist"] = data["enforce_artist_equals_album_artist"]
            if isinstance(data.get("fetch_art_online"), bool):
                settings["fetch_art_online"] = data["fetch_art_online"]
            if isinstance(data.get("art_sources"), dict):
                for key, value in data["art_sources"].items():
                    if key in _VALID_ART_SOURCES and isinstance(value, bool):
                        settings["art_sources"][key] = value
            if isinstance(data.get("art_source_order"), list):
                order = [s for s in data["art_source_order"] if s in _VALID_ART_SOURCES]
                order += [s for s in ART_SOURCE_ORDER if s not in order]
                settings["art_source_order"] = order
            if isinstance(data.get("theaudiodb_api_key"), str):
                settings["theaudiodb_api_key"] = data["theaudiodb_api_key"].strip()
            if isinstance(data.get("discogs_token"), str):
                settings["discogs_token"] = data["discogs_token"].strip()
        except Exception:
            pass
    return settings


def save(library_root: Path, settings: dict) -> None:
    path = library_root / SETTINGS_FILENAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: settings[k] for k in DEFAULTS if k in settings}, f, indent=2)
