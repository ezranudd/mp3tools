"""
Library settings — self-contained in a hidden per-library folder.

Everything a library needs lives under {library_root}/.mp3tools/:
  - mp3tools.conf  : settings JSON (was the old {library_root}/.mp3tools file)
  - background     : web UI background image (was {library_root}/.mp3tools-background)

load()/save() transparently migrate the old single-file layout the first time
they run against a library.
"""
import json
import copy
from pathlib import Path

SETTINGS_DIRNAME = ".mp3tools"
CONF_FILENAME = "mp3tools.conf"
BACKGROUND_FILENAME = "background"

# Old (pre-folder) locations, migrated on first load/save.
_LEGACY_SETTINGS_FILENAME = ".mp3tools"            # a JSON file at the library root
_LEGACY_BACKGROUND_FILENAME = ".mp3tools-background"

ART_SOURCE_ORDER = ["itunes", "musicbrainz", "theaudiodb", "discogs"]


def settings_dir(library_root: Path) -> Path:
    return library_root / SETTINGS_DIRNAME


def conf_path(library_root: Path) -> Path:
    return settings_dir(library_root) / CONF_FILENAME


def background_path(library_root: Path) -> Path:
    return settings_dir(library_root) / BACKGROUND_FILENAME


def _migrate(library_root: Path) -> None:
    """Move the old single-file layout into the .mp3tools/ folder. Best-effort:
    any failure leaves the old files untouched rather than raising."""
    try:
        legacy = library_root / _LEGACY_SETTINGS_FILENAME
        # Old format: .mp3tools is a regular file. Convert it (same name) to a
        # directory holding mp3tools.conf — so read+remove before mkdir.
        if legacy.is_file():
            data = legacy.read_bytes()
            legacy.unlink()
            settings_dir(library_root).mkdir(exist_ok=True)
            dest = conf_path(library_root)
            if not dest.exists():
                dest.write_bytes(data)

        legacy_bg = library_root / _LEGACY_BACKGROUND_FILENAME
        if legacy_bg.is_file():
            settings_dir(library_root).mkdir(exist_ok=True)
            target = background_path(library_root)
            if target.exists():
                legacy_bg.unlink()
            else:
                legacy_bg.replace(target)
    except Exception:
        pass

DEFAULTS: dict = {
    "cover_art":            "folder",  # "folder" | "embed" | "both"
    "cover_art_embed_size": 500,       # pixels; 0 = no resize
    "enforce_artist_equals_album_artist": False,
    "replace_brackets_with_parentheses":  False,
    "fetch_art_online":     False,     # run step 15 during standardize
    "preserve_replay_gain": False,    # keep TXXX:REPLAYGAIN_* during step 4
    "preserve_tcmp":        False,    # keep/set TCMP=1 (iTunes compilation) during step 4
    "preserve_disc_numbers": False,   # write TPOS on merge; keep per-disc TRCK in steps 4/7/8
    "eject_cd_after_import": False,   # eject disc when CD import finishes
    "art_sources": {
        "itunes":       True,
        "musicbrainz":  True,
        "theaudiodb":   False,
        "discogs":      False,         # browse only; never used by standardize batch
    },
    "art_source_order": list(ART_SOURCE_ORDER),
    "theaudiodb_api_key": "",
    "discogs_token":      "",
    "background_opacity": 0.4,        # web UI: scrim strength over the bg image, 0..1
    "background_blur":    0,          # web UI: bg image blur in px, 0..40
    "background_fit":     "cover",    # web UI: "cover" | "contain" | "tile"
    "background_mime":    "",         # web UI: mime of the uploaded bg image (set on upload)
    "background_readable": True,      # web UI: boost text contrast over the bg image
    "import_bitrate":       320,      # web UI: default lossless→MP3 bitrate for import
}

_VALID_BITRATES = frozenset((128, 160, 192, 256, 320))

_VALID_COVER_ART = frozenset(("folder", "embed", "both"))
_VALID_ART_SOURCES = frozenset(ART_SOURCE_ORDER)
_VALID_BACKGROUND_FIT = frozenset(("cover", "contain", "tile"))


def load(library_root: Path) -> dict:
    _migrate(library_root)
    settings = copy.deepcopy(DEFAULTS)
    path = conf_path(library_root)
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
            if isinstance(data.get("replace_brackets_with_parentheses"), bool):
                settings["replace_brackets_with_parentheses"] = data["replace_brackets_with_parentheses"]
            if isinstance(data.get("fetch_art_online"), bool):
                settings["fetch_art_online"] = data["fetch_art_online"]
            if isinstance(data.get("preserve_replay_gain"), bool):
                settings["preserve_replay_gain"] = data["preserve_replay_gain"]
            if isinstance(data.get("preserve_tcmp"), bool):
                settings["preserve_tcmp"] = data["preserve_tcmp"]
            if isinstance(data.get("preserve_disc_numbers"), bool):
                settings["preserve_disc_numbers"] = data["preserve_disc_numbers"]
            if isinstance(data.get("eject_cd_after_import"), bool):
                settings["eject_cd_after_import"] = data["eject_cd_after_import"]
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
            if isinstance(data.get("background_opacity"), (int, float)):
                settings["background_opacity"] = min(1.0, max(0.0, float(data["background_opacity"])))
            if isinstance(data.get("background_blur"), (int, float)):
                settings["background_blur"] = min(40, max(0, int(data["background_blur"])))
            if data.get("background_fit") in _VALID_BACKGROUND_FIT:
                settings["background_fit"] = data["background_fit"]
            if isinstance(data.get("background_mime"), str):
                settings["background_mime"] = data["background_mime"].strip()
            if isinstance(data.get("background_readable"), bool):
                settings["background_readable"] = data["background_readable"]
            if data.get("import_bitrate") in _VALID_BITRATES:
                settings["import_bitrate"] = data["import_bitrate"]
        except Exception:
            pass
    return settings


def save(library_root: Path, settings: dict) -> None:
    _migrate(library_root)
    settings_dir(library_root).mkdir(parents=True, exist_ok=True)
    path = conf_path(library_root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: settings[k] for k in DEFAULTS if k in settings}, f, indent=2)
