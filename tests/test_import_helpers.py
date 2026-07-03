"""
Pure-function tests for import_tracks.py helpers (no ffmpeg).
"""
from pathlib import Path

import pytest

from import_tracks import (
    _disc_track_nums,
    _natural_key,
    extract_year,
    merge_order_key,
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
    # The total survives a whitespace-only track number.
    assert parse_track("  /12") == (None, 12)


# ── _natural_key ──────────────────────────────────────────────────────────────

def test_natural_key_numeric_ordering():
    assert _natural_key("2") < _natural_key("10")
    assert _natural_key("CD2") < _natural_key("CD10")
    assert _natural_key("track 9") < _natural_key("track 11")


def test_natural_key_case_insensitive_and_comparable():
    assert _natural_key("ABC") == _natural_key("abc")
    # Mixed digit/text chunks stay comparable (uniformly-typed tuples).
    assert sorted(["a1", "1a", "a", "1"], key=_natural_key) == ["1", "1a", "a", "a1"]


# ── _disc_track_nums ──────────────────────────────────────────────────────────

def test_disc_track_nums():
    assert _disc_track_nums({"TPOS": "2/3", "TRCK": "5/12"}) == (2, 5)
    assert _disc_track_nums({}) == (1, 10**9)          # missing → disc 1, sentinel track
    assert _disc_track_nums({"TPOS": "x", "TRCK": "y"}) == (1, 10**9)
    assert _disc_track_nums({"TRCK": "07"}) == (1, 7)


# ── merge_order_key ───────────────────────────────────────────────────────────

def _key(source, rel, td=None):
    return merge_order_key(source, source / rel, td or {})


def test_merge_order_parent_files_before_subfolders():
    src = Path("/src")
    parent = _key(src, "01. a.mp3", {"TRCK": "1"})
    child = _key(src, "Bonus/01. b.mp3", {"TRCK": "1"})
    assert parent < child


def test_merge_order_disc_folders_naturally_sorted():
    src = Path("/src")
    cd1 = _key(src, "CD1/x.mp3")
    cd2 = _key(src, "CD2/x.mp3")
    cd10 = _key(src, "CD10/x.mp3")
    assert cd1 < cd2 < cd10


def test_merge_order_within_folder_by_disc_then_track():
    src = Path("/src")
    d1t2 = _key(src, "a.mp3", {"TPOS": "1", "TRCK": "2"})
    d1t10 = _key(src, "b.mp3", {"TPOS": "1", "TRCK": "10"})
    d2t1 = _key(src, "c.mp3", {"TPOS": "2", "TRCK": "1"})
    assert d1t2 < d1t10 < d2t1


def test_merge_order_untagged_falls_back_to_filename():
    src = Path("/src")
    a = _key(src, "01 first.mp3")
    b = _key(src, "02 second.mp3")
    assert a < b


def test_merge_order_file_outside_source():
    # A src not under source falls back to its bare name instead of raising.
    key = merge_order_key(Path("/src"), Path("/elsewhere/track.mp3"), {})
    assert key[-1][0] == 0   # still a file-leaf component
