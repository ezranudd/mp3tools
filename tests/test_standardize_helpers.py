"""
Pure-function tests for standardize.py helpers (no ffmpeg).
"""
from pathlib import Path

import pytest

from standardize import (
    _is_disc_folder,
    _subfolder_sort_key,
    extract_year,
    parse_track,
)

from conftest import TRACK_CASES, YEAR_CASES


@pytest.mark.parametrize("raw,expected", YEAR_CASES)
def test_extract_year(raw, expected):
    assert extract_year(raw) == expected


@pytest.mark.parametrize("raw,expected", TRACK_CASES)
def test_parse_track(raw, expected):
    assert parse_track(raw) == expected


def test_parse_track_blank_num_with_total():
    # Shared implementation strips before the emptiness check, so the total
    # survives a whitespace-only track number.
    assert parse_track("  /12") == (None, 12)


def test_is_disc_folder():
    assert _is_disc_folder(Path("CD1"))
    assert _is_disc_folder(Path("cd 02"))
    assert _is_disc_folder(Path("Disc 2"))
    assert _is_disc_folder(Path("2"))                # bare number
    assert not _is_disc_folder(Path("Bonus Tracks"))
    assert not _is_disc_folder(Path("Extras"))


def test_subfolder_sort_key_numbers_before_names():
    # Numbered disc folders order by their last number; unnumbered folders
    # (bonus material) sort after them, alphabetically.
    folders = [Path("Bonus"), Path("CD10"), Path("CD2"), Path("Disc 1"), Path("Appendix")]
    ordered = sorted(folders, key=_subfolder_sort_key)
    assert [p.name for p in ordered] == ["Disc 1", "CD2", "CD10", "Appendix", "Bonus"]
