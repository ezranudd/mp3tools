"""
chars.py table plus cross-module drift checks for the normalize/sanitize
copies in audit.py, standardize.py, import_tracks.py, and browse.py.
"""
import unicodedata

import pytest

import audit
import browse
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


def test_audit_sanitize_skips_normalization():
    # Verified drift: audit.sanitize is substitution-only — its callers apply
    # sanitize(normalize(...)) — while the other three normalize internally.
    assert audit.sanitize("don’t") == "don’t"
    for fn in (standardize.sanitize_name, import_tracks.sanitize_name, browse._sanitize):
        assert fn("don’t") == "don't"


def test_browse_normalize_applies_nfc_others_dont():
    decomposed = "Cafe" + "́"                       # e + combining acute
    composed = unicodedata.normalize("NFC", decomposed)  # é as one code point
    assert browse._normalize(decomposed) == composed
    # Verified drift: the other copies leave the decomposed form untouched.
    assert standardize.normalize_string(decomposed) == decomposed
    assert audit.normalize(decomposed) == decomposed
    assert import_tracks.normalize_string(decomposed) == decomposed
