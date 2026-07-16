"""
Named encoding profiles — the single source of truth for how new lossless→lossy
encodes are produced (import, standardize step 0). One registry spanning two
formats (Opus, MP3); adding a format later is one entry + one encode branch in
convert_lossless.py.

Consumed by settings.py (validation + legacy migration), webjobs.py / server.py
(payloads to the UI), import_tracks.py and convert_lossless.py (the encode path),
and the frontend (grouped dropdowns). Encoder flags live on the profile so no
consumer hardcodes a bitrate or codec.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EncodeProfile:
    id: str                                    # "opus-128"
    fmt: str                                   # "opus" | "mp3"
    ext: str                                   # ".opus" | ".mp3"
    label: str                                 # UI label
    opus_bitrate: int | None = None            # opus profiles: VBR target kbps
    lame_args: tuple[str, ...] | None = None   # mp3 profiles: the encoder-arg
    #                                            fragment replacing `--cbr -b N`


# Insertion order is the UI order (Opus tiers first, then MP3 fallbacks).
PROFILES: dict[str, EncodeProfile] = {
    p.id: p for p in (
        EncodeProfile("opus-160", "opus", ".opus",
                      "Opus 160 — paranoid transparent", opus_bitrate=160),
        EncodeProfile("opus-128", "opus", ".opus",
                      "Opus 128 — transparent", opus_bitrate=128),
        EncodeProfile("opus-96", "opus", ".opus",
                      "Opus 96 — space saving (music)", opus_bitrate=96),
        EncodeProfile("opus-64", "opus", ".opus",
                      "Opus 64 — recordings / spoken word", opus_bitrate=64),
        EncodeProfile("mp3-v0", "mp3", ".mp3",
                      "MP3 V0 — transparent (default MP3)", lame_args=("-V", "0")),
        EncodeProfile("mp3-320", "mp3", ".mp3",
                      "MP3 320 CBR — maximum compatibility",
                      lame_args=("--cbr", "-b", "320")),
    )
}

DEFAULT_PROFILE = "opus-128"

# Legacy settings migration: the old raw-int `import_bitrate` → a profile id.
# 320 kept its exact intent (mp3-320); the lower CBR tiers collapse to MP3 V0
# (closest surviving MP3 quality). We never silently change a user's *format*
# to Opus — that is worse than changing a bitrate. (See settings.load().)
LEGACY_BITRATE_TO_PROFILE = {
    320: "mp3-320",
    256: "mp3-v0",
    192: "mp3-v0",
    160: "mp3-v0",
    128: "mp3-v0",
}


def is_valid(profile_id) -> bool:
    return profile_id in PROFILES


def get(profile_id) -> EncodeProfile:
    """The profile for *profile_id*, or the default for an unknown/None id."""
    return PROFILES.get(profile_id) or PROFILES[DEFAULT_PROFILE]


def profiles_json() -> list[dict]:
    """The registry as the UI consumes it (ordered): id, fmt, label."""
    return [{"id": p.id, "fmt": p.fmt, "label": p.label} for p in PROFILES.values()]
