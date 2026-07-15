"""
Format-agnostic tag interface for the library tools (audit, standardize).

One neutral tag model + per-format backends, so audit.py and standardize.py can
work on any audio format without touching mutagen frame internals. The MP3/ID3
backend reproduces the tools' historical behaviour exactly (the pytest suite
enforces byte-identical MP3 output); the Opus/Vorbis backend is added alongside.

Public surface:
  open_audio(path) -> AudioTags | None        # dispatch on extension; None on error
  register(ext, backend)                        # extend with a new format
  AudioTags (ABC): read/write/covers/info/diagnostics

Canonical model (dict, plain str | None values — never mutagen objects/lists):
  title, artist, album_artist, album, date, genre, track, disc
  - track/disc are "n" or "n/total" (the form chars.parse_track consumes).
  - date is the raw year/timestamp string (MP3: TYER or TDRC; Opus: DATE).

Format-specific facts that don't fit the neutral model (ID3 version, ID3v1
presence, legacy album-artist frames, raw TYER/TDRC) are exposed via
diagnostics(); consumers read the keys they know and never branch on format.

The historical ID3 leaf helpers (load_id3, album_artist_value, set_album_artist,
has_id3v1, has_embedded_art, mp3_read_framekey) live here as module functions so
audit/standardize can re-export them and hold no direct mutagen.id3 code.
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from pathlib import Path

from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    APIC, TPE1, TPE2, TIT2, TALB, TYER, TCON, TRCK, TPOS,
)
from mutagen.mp3 import MP3

from chars import AUDIO_EXTENSIONS  # noqa: F401  (re-exported for consumers)

# Canonical tag keys (the neutral model). Order is display/iteration order.
CANONICAL_KEYS = ("title", "artist", "album_artist", "album",
                  "date", "genre", "track", "disc")

# TPE2 is the canonical album-artist frame. The TXXX variants are legacy — read
# them as fallbacks for migration, but set_album_artist never writes them.
ALBUM_ARTIST_KEYS = (
    "TPE2",
    "TXXX:album artist",
    "TXXX:ALBUMARTIST",
    "TXXX:ALBUM ARTIST",
    "TXXX:AlbumArtist",
    "TXXX:Album Artist",
)


# ── ID3 leaf helpers (the MP3 backend's internals; re-exported by consumers) ───

def load_id3(path: Path) -> ID3:
    """Load raw ID3 frames without mutagen's v2.4 translation layer."""
    return ID3(path, translate=False)


def album_artist_value(tags: ID3) -> str | None:
    """Canonical album artist: TPE2, then legacy TXXX variants (first non-empty)."""
    for key in ALBUM_ARTIST_KEYS:
        frame = tags.get(key)
        if frame and hasattr(frame, "text") and frame.text:
            return str(frame.text[0])
    return None


def set_album_artist(tags: ID3, value: str) -> None:
    """Write the album artist to TPE2 (canonical) and delete every legacy TXXX
    variant, so the file ends up with exactly one album-artist frame."""
    for key in ALBUM_ARTIST_KEYS:
        if key != "TPE2" and key in tags:
            del tags[key]
    tags["TPE2"] = TPE2(encoding=1, text=value)


def has_id3v1(path: Path) -> bool:
    """True if the file carries an ID3v1 tag (last 128 bytes start with b'TAG')."""
    try:
        with open(path, "rb") as f:
            f.seek(-128, 2)
            return f.read(3) == b"TAG"
    except OSError:
        return False


def has_embedded_art(path: Path) -> bool:
    """True if the MP3 has at least one APIC (embedded image) frame."""
    try:
        tags = load_id3(path)
        return any(k.startswith("APIC") for k in tags.keys())
    except Exception:
        return False


def mp3_read_framekey(path: Path) -> dict | None:
    """The legacy frame-key tag dict (audit's historical read_tags contract):
    raw ID3 frame keys + `_version` + `_legacy_albumartist`, or None on a genuine
    read error. A tagless-but-valid MP3 yields an all-None dict, not None."""
    keys = ("TPE1", "TPE2", "TIT2", "TALB", "TYER", "TDRC", "TCON", "TRCK")
    try:
        tags = load_id3(path)
        result = {
            k: (str(tags[k].text[0]) if k in tags and hasattr(tags[k], "text") else None)
            for k in keys
        }
        result["ALBUMARTIST"] = album_artist_value(tags)
        result["_version"] = tags.version
        result["_legacy_albumartist"] = any(
            k in tags for k in ALBUM_ARTIST_KEYS if k != "TPE2"
        )
        return result
    except ID3NoHeaderError:
        result = {k: None for k in keys}
        result["ALBUMARTIST"] = None
        result["_version"] = None
        result["_legacy_albumartist"] = False
        return result
    except Exception:
        return None


# ── The interface ─────────────────────────────────────────────────────────────

def _empty_canonical() -> dict:
    return {k: None for k in CANONICAL_KEYS}


class AudioTags(ABC):
    """One audio file's tags, read/written through the neutral model."""

    format: str

    @abstractmethod
    def read(self) -> dict: ...

    @abstractmethod
    def write(self, updates: dict) -> None: ...

    @abstractmethod
    def get_cover(self) -> tuple[bytes, str] | None: ...

    @abstractmethod
    def set_cover(self, data: bytes, mime: str) -> None: ...

    @abstractmethod
    def remove_cover(self) -> None: ...

    @abstractmethod
    def has_cover(self) -> bool: ...

    @abstractmethod
    def info(self) -> dict: ...

    @abstractmethod
    def diagnostics(self) -> dict: ...


# ── MP3 / ID3 backend ─────────────────────────────────────────────────────────

class Mp3Tags(AudioTags):
    """ID3v2.3 backend. Reproduces the tools' historical MP3 behaviour exactly:
    reads with translate=False, writes encoding=1 frames and saves
    v2_version=3, v1=0; album artist canonical in TPE2; cover in APIC 'APIC:'."""

    format = "mp3"

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self._tags = load_id3(self.path)
            self._has_header = True
        except ID3NoHeaderError:
            self._tags = ID3()
            self._has_header = False
        # Any other exception propagates → open_audio() maps it to None.

    def _g(self, key: str) -> str | None:
        f = self._tags.get(key)
        return str(f.text[0]) if f is not None and getattr(f, "text", None) else None

    def read(self) -> dict:
        return {
            "title":        self._g("TIT2"),
            "artist":       self._g("TPE1"),
            "album_artist": album_artist_value(self._tags),
            "album":        self._g("TALB"),
            "date":         self._g("TYER") or self._g("TDRC"),
            "genre":        self._g("TCON"),
            "track":        self._g("TRCK"),
            "disc":         self._g("TPOS"),
        }

    _FRAME = {"title": TIT2, "artist": TPE1, "album": TALB,
              "date": TYER, "genre": TCON, "track": TRCK, "disc": TPOS}

    def write(self, updates: dict) -> None:
        for key, val in updates.items():
            if key == "album_artist":
                if val is not None:
                    set_album_artist(self._tags, str(val))
                continue
            cls = self._FRAME.get(key)
            if cls is None or val is None:
                continue
            self._tags[cls.__name__] = cls(encoding=1, text=str(val))
        self._tags.save(self.path, v2_version=3, v1=0)

    # ── Cover art (APIC 'APIC:', front cover) ──
    def get_cover(self) -> tuple[bytes, str] | None:
        apic = self._tags.get("APIC:") or next(
            (self._tags[k] for k in self._tags if k.startswith("APIC")), None)
        if apic is not None and getattr(apic, "data", None):
            return apic.data, (getattr(apic, "mime", "image/jpeg") or "image/jpeg")
        return None

    def set_cover(self, data: bytes, mime: str) -> None:
        self._tags["APIC:"] = APIC(encoding=3, mime=mime, type=3, desc="", data=data)
        self._tags.save(self.path, v2_version=3, v1=0)

    def remove_cover(self) -> None:
        for k in [k for k in self._tags if k.startswith("APIC")]:
            del self._tags[k]
        self._tags.save(self.path, v2_version=3, v1=0)

    def has_cover(self) -> bool:
        return any(k.startswith("APIC") for k in self._tags.keys())

    def info(self) -> dict:
        try:
            i = MP3(self.path).info
            return {"bitrate_kbps": int(i.bitrate / 1000) if i.bitrate else None,
                    "length_sec": float(i.length) if i.length else None}
        except Exception:
            return {"bitrate_kbps": None, "length_sec": None}

    def diagnostics(self) -> dict:
        return {
            "id3_version":        self._tags.version if self._has_header else None,
            "legacy_albumartist": any(k in self._tags for k in ALBUM_ARTIST_KEYS
                                      if k != "TPE2"),
            "has_id3v1":          has_id3v1(self.path),
            "tyer":               self._g("TYER"),
            "tdrc":               self._g("TDRC"),
        }


# ── Dispatch registry ─────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[AudioTags]] = {".mp3": Mp3Tags}


def register(ext: str, backend: type[AudioTags]) -> None:
    """Register a backend for a file extension (e.g. register('.opus', OpusTags))."""
    _REGISTRY[ext.lower()] = backend


def open_audio(path) -> AudioTags | None:
    """Open *path* with the backend for its extension. Returns None for an
    unknown extension or an unreadable/corrupt file (a tagless-but-valid file
    opens fine — its read() is all-None). Mirrors the old read_tags None contract."""
    p = Path(path)
    backend = _REGISTRY.get(p.suffix.lower())
    if backend is None:
        return None
    try:
        return backend(p)
    except Exception:
        return None
