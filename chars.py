"""
Canonical character handling, shared across every module that normalizes tag
values and filenames (audit, standardize, import_tracks, browse).

These helpers were previously duplicated per module with slight drift (only
browse applied NFC; audit's sanitize skipped the quote table; standardize's
parse_track handled whitespace differently). They now live here once, and the
modules import them under their historical local names.
"""

import re
import unicodedata

# Typographic apostrophes → ASCII '  and curly quotes → ASCII "  (U+2018–U+201F).
CHAR_REPLACEMENTS: dict[str, str] = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
}

# Filesystem-unsafe character substitutions (files and folders).
_SANITIZE_REPLACEMENTS: dict[str, str] = {
    "/": "-", "\\": "-", ":": " -", "*": "",
    "?": "", '"': "'", "<": "", ">": "", "|": "-",
}

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def normalize(s: str) -> str:
    """Compose to Unicode NFC, then replace typographic quotes with ASCII."""
    s = unicodedata.normalize("NFC", s)
    for old, new in CHAR_REPLACEMENTS.items():
        s = s.replace(old, new)
    return s


def needs_normalization(s: str) -> bool:
    """True when normalize() would change *s* (table chars or non-NFC form)."""
    return normalize(s) != s


def sanitize(name: str) -> str:
    """Filesystem-safe name: normalize, strip unsafe chars, trim trailing '. '."""
    name = normalize(name)
    for old, new in _SANITIZE_REPLACEMENTS.items():
        name = name.replace(old, new)
    return name.rstrip(". ")


def extract_year(value: str) -> str | None:
    """First 19xx/20xx year in *value*, or None."""
    m = _YEAR_RE.search(str(value))
    return m.group(1) if m else None


def parse_track(s: str) -> tuple[int | None, int | None]:
    """Parse "N" or "N/T" → (num, total); each part None when absent/invalid."""
    parts = s.split("/")
    try:
        n = int(parts[0].strip()) if parts[0].strip() else None
        t = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
        return n, t
    except ValueError:
        return None, None
