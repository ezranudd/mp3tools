"""
Pure-function tests for browse.py helpers (no ffmpeg).
"""
from browse import _fmt_dur, _track_label


def test_fmt_dur():
    assert _fmt_dur(None) == ""
    assert _fmt_dur(0) == ""
    assert _fmt_dur(-3) == ""
    assert _fmt_dur(59.9) == "0:59"
    assert _fmt_dur(61) == "1:01"
    assert _fmt_dur(3661) == "61:01"   # minutes keep growing — no hours unit


def test_track_label_fallbacks():
    assert _track_label({}, "file.mp3") == "file.mp3"          # no title → filename
    assert _track_label({"title": "T"}, "x") == "T"            # no track number
    assert _track_label({"title": "T", "track": "3/12"}, "x") == "03. T"
    assert _track_label({"title": "T", "track": "12"}, "x") == "12. T"
    assert _track_label({"title": "T", "track": "A1"}, "x") == "A1. T"  # non-digit kept as-is
