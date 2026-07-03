"""
Pure-function tests for audit.py helpers (no ffmpeg).
"""
import pytest

from audit import (
    build_expected_filename,
    build_expected_folder,
    extract_year,
    parse_track,
    year_from_tags,
)

from conftest import TRACK_CASES, YEAR_CASES


@pytest.mark.parametrize("raw,expected", YEAR_CASES)
def test_extract_year(raw, expected):
    assert extract_year(raw) == expected


@pytest.mark.parametrize("raw,expected", TRACK_CASES)
def test_parse_track(raw, expected):
    assert parse_track(raw) == expected


def test_parse_track_blank_num_with_total():
    # The total survives a whitespace-only track number.
    assert parse_track("  /12") == (None, 12)


def test_year_from_tags_prefers_tyer():
    assert year_from_tags({"TYER": "2001", "TDRC": "1999"}) == "2001"
    assert year_from_tags({"TYER": None, "TDRC": "1999"}) == "1999"
    assert year_from_tags({}) is None


# ── build_expected_filename ───────────────────────────────────────────────────

def test_expected_filename_basic():
    tags = {"TPE1": "Artist", "TIT2": "Title", "TRCK": "5/12"}
    assert build_expected_filename(tags, 2) == "05. Artist - Title.mp3"


def test_expected_filename_width_3():
    tags = {"TPE1": "Artist", "TIT2": "Title", "TRCK": "7/120"}
    assert build_expected_filename(tags, 3) == "007. Artist - Title.mp3"


def test_expected_filename_sanitizes_and_normalizes():
    tags = {"TPE1": "AC/DC", "TIT2": "Don’t Stop", "TRCK": "1/1"}
    assert build_expected_filename(tags, 2) == "01. AC-DC - Don't Stop.mp3"


@pytest.mark.parametrize("missing", ["TPE1", "TIT2", "TRCK"])
def test_expected_filename_missing_tag_is_none(missing):
    tags = {"TPE1": "A", "TIT2": "T", "TRCK": "1/1"}
    tags[missing] = None
    assert build_expected_filename(tags, 2) is None


def test_expected_filename_unparseable_track_is_none():
    assert build_expected_filename({"TPE1": "A", "TIT2": "T", "TRCK": "x"}, 2) is None


# ── build_expected_folder ─────────────────────────────────────────────────────

def test_expected_folder_basic():
    tags = [{"TYER": "2024", "TALB": "Album"}] * 2
    assert build_expected_folder(tags) == "2024 - Album"


def test_expected_folder_majority_vote():
    # The most common year/album across tracks wins over an outlier.
    tags = [{"TYER": "2024", "TALB": "Album"},
            {"TYER": "2024", "TALB": "Album"},
            {"TYER": "1999", "TALB": "Other"}]
    assert build_expected_folder(tags) == "2024 - Album"


def test_expected_folder_tdrc_fallback_and_sanitize():
    tags = [{"TDRC": "1999-05-01", "TALB": "Live: At Home"}]
    assert build_expected_folder(tags) == "1999 - Live - At Home"


def test_expected_folder_missing_parts_is_none():
    assert build_expected_folder([{"TALB": "Album"}]) is None       # no year
    assert build_expected_folder([{"TYER": "2024"}]) is None        # no album
    assert build_expected_folder([]) is None
