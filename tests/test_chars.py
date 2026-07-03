"""
chars.py shared helpers, plus guards that audit.py, standardize.py,
import_tracks.py, and browse.py all alias the same implementations.
"""
import unicodedata

import pytest

import audit
import browse
import chars
import import_tracks
import standardize
from chars import CHAR_REPLACEMENTS

from conftest import NORMALIZE_CASES, SANITIZE_CASES

NORMALIZERS = [
    pytest.param(standardize.normalize_string, id="standardize"),
    pytest.param(import_tracks.normalize_string, id="import_tracks"),
    pytest.param(audit.normalize, id="audit"),
    pytest.param(browse._normalize, id="browse"),
]

SANITIZERS = [
    pytest.param(standardize.sanitize_name, id="standardize"),
    pytest.param(import_tracks.sanitize_name, id="import_tracks"),
    pytest.param(audit.sanitize, id="audit"),
    pytest.param(browse._sanitize, id="browse"),
]


def test_char_replacements_table():
    # Exactly the eight Unicode quotation marks U+2018–U+201F, mapped to ASCII.
    assert set(CHAR_REPLACEMENTS) == {chr(c) for c in range(0x2018, 0x2020)}
    assert set(CHAR_REPLACEMENTS.values()) == {"'", '"'}
    assert all(CHAR_REPLACEMENTS[chr(c)] == "'" for c in range(0x2018, 0x201C))
    assert all(CHAR_REPLACEMENTS[chr(c)] == '"' for c in range(0x201C, 0x2020))


@pytest.mark.parametrize("fn", NORMALIZERS)
@pytest.mark.parametrize("raw,expected", NORMALIZE_CASES)
def test_normalize_copies_agree(fn, raw, expected):
    assert fn(raw) == expected


@pytest.mark.parametrize("fn", SANITIZERS)
@pytest.mark.parametrize("raw,expected", SANITIZE_CASES)
def test_sanitize_copies_agree(fn, raw, expected):
    assert fn(raw) == expected


def test_has_special_chars_copies_agree():
    for fn in (standardize.has_special_chars, audit.has_nonstandard_chars):
        assert fn("don’t") is True
        assert fn("“x”") is True
        assert not fn("don't")
        assert not fn("")


def test_all_modules_share_the_chars_helpers():
    # The per-module names are aliases of the single chars.py implementation —
    # drift is impossible by construction.
    assert (standardize.normalize_string is import_tracks.normalize_string
            is audit.normalize is browse._normalize is chars.normalize)
    assert (standardize.sanitize_name is import_tracks.sanitize_name
            is audit.sanitize is browse._sanitize is chars.sanitize)
    assert (standardize.extract_year is import_tracks.extract_year
            is audit.extract_year is browse._extract_year is chars.extract_year)
    assert (standardize.parse_track is import_tracks.parse_track
            is audit.parse_track is chars.parse_track)
    assert (standardize.has_special_chars is audit.has_nonstandard_chars
            is chars.needs_normalization)


def test_normalize_applies_nfc_everywhere():
    decomposed = "Cafe" + "́"                       # e + combining acute
    composed = unicodedata.normalize("NFC", decomposed)  # é as one code point
    assert composed != decomposed
    for fn in (standardize.normalize_string, import_tracks.normalize_string,
               audit.normalize, browse._normalize):
        assert fn(decomposed) == composed
    assert chars.sanitize(decomposed) == composed


def test_needs_normalization():
    assert chars.needs_normalization("don’t") is True     # table char
    assert chars.needs_normalization("Cafe" + "́") is True  # non-NFC form
    assert chars.needs_normalization("don't") is False
    assert chars.needs_normalization("café") is False      # already NFC
    assert chars.needs_normalization("") is False
