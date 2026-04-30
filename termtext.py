"""Terminal cell-width helpers backed by wcwidth.

All layout that depends on visible column width should use these instead of
len(), [:n], or .ljust(n), which count codepoints rather than display cells.
"""

import unicodedata

from wcwidth import wcwidth as _wcwidth


def cell_width(s: str) -> int:
    """Return the number of terminal columns needed to display s."""
    total = 0
    for ch in s:
        w = _wcwidth(ch)
        if w > 0:
            total += w
    return total


def clip_cells(s: str, width: int, ellipsis: str = "") -> str:
    """Return a prefix of s whose display width fits within *width* columns.

    If *ellipsis* is given and the string had to be shortened, the ellipsis is
    appended (its own width is subtracted from the budget first).
    """
    if width <= 0:
        return ""
    budget = width - cell_width(ellipsis)
    if budget < 0:
        budget = 0
    cols = 0
    for i, ch in enumerate(s):
        cw = max(0, _wcwidth(ch))
        if cols + cw > budget:
            return s[:i] + ellipsis
        cols += cw
    return s


def pad_cells(s: str, width: int) -> str:
    """Pad s with trailing spaces so its display width equals *width* columns."""
    return s + " " * max(0, width - cell_width(s))


def fit_cells(s: str, width: int) -> str:
    """Clip s to *width* columns then pad to exactly *width* columns."""
    return pad_cells(clip_cells(s, width), width)
