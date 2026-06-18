"""
Canonical character-normalization table, shared across the modules that
normalize tag values and filenames.

This table was previously duplicated in standardize.py, audit.py, browse.py,
and import_tracks.py with a standing "keep all four in sync" warning. It now
lives here so it cannot drift.

Note: the per-module normalize functions still differ slightly — browse applies
`unicodedata.normalize("NFC", ...)` before the table substitutions while the
others do not. Only the table is shared here; unifying that NFC step is a
separate behavior decision.
"""

# Typographic apostrophes → ASCII '  and curly quotes → ASCII "  (U+2018–U+201F).
CHAR_REPLACEMENTS: dict[str, str] = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
}
