"""
Pure-function tests for fetch_art.py (no network — matching/scoring helpers only).
"""


def test_strip_edition():
    from fetch_art import _strip_edition
    assert _strip_edition("Album (Deluxe Edition)") == "Album"
    assert _strip_edition("Album [Remastered]") == "Album"
    assert _strip_edition("Album - Remastered 2011") == "Album"
    assert _strip_edition("Album (Disc 2)") == "Album"
    assert _strip_edition("Album") == "Album"                 # unchanged
    assert _strip_edition("(Deluxe Edition)") == "(Deluxe Edition)"  # don't empty it


def test_match_score_edition_tolerant():
    from fetch_art import _match_score, CONFIDENT_MATCH_SCORE
    # Deluxe-vs-plain (artist exact) still clears the confident bar.
    assert _match_score("Artist", "Album", "Artist", "Album (Deluxe Edition)") \
        >= CONFIDENT_MATCH_SCORE
    assert _match_score("Artist", "Album (Deluxe Edition)", "Artist", "Album") \
        >= CONFIDENT_MATCH_SCORE
    # A small typo still scores via the fuzzy fallback, above a clearly different album.
    typo = _match_score("Artist", "Album", "Artist", "Albbum")
    other = _match_score("Artist", "Album", "Artist", "Completely Different")
    assert typo > other
