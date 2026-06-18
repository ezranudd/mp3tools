"""
Shared UI foundation for the curses modules (tui, browse, sync_library,
import_preview).

This module is the single source of truth for the color palette so that every
screen uses the same pair numbers and colors. Previously browse.py owned pairs
1-7, tui.py added 8-11, and sync_library.py defined its own conflicting 1-6 set
that had to be re-initialized before every use. All of them now import from here.
"""

import curses

# ── Canonical color pairs ─────────────────────────────────────────────────────
C_ARTIST = 1   # bold yellow
C_ALBUM  = 2   # cyan
C_TRACK  = 3   # default fg
C_HDR    = 4   # white on blue   (header bar)
C_BAR    = 5   # black on cyan   (status/footer bar)
C_DIM    = 6   # dim white       (aside counts)
C_EDIT   = 7   # magenta         (pending-edit preview nodes)
C_SEL    = 8   # black on white  (selected row)
C_OK     = 9   # green
C_WARN   = 10  # yellow
C_ERR    = 11  # red
C_FMT    = 12  # green           (format badge, e.g. [FLAC], in import preview)


def init_colors() -> None:
    """Initialize all color pairs. Safe to call repeatedly and on terminals
    without color support (errors are swallowed)."""
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_ARTIST, curses.COLOR_YELLOW,  -1)
        curses.init_pair(C_ALBUM,  curses.COLOR_CYAN,    -1)
        curses.init_pair(C_TRACK,  -1,                   -1)
        curses.init_pair(C_HDR,    curses.COLOR_WHITE,   curses.COLOR_BLUE)
        curses.init_pair(C_BAR,    curses.COLOR_BLACK,   curses.COLOR_CYAN)
        curses.init_pair(C_DIM,    curses.COLOR_WHITE,   -1)
        curses.init_pair(C_EDIT,   curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_SEL,    curses.COLOR_BLACK,   curses.COLOR_WHITE)
        curses.init_pair(C_OK,     curses.COLOR_GREEN,   -1)
        curses.init_pair(C_WARN,   curses.COLOR_YELLOW,  -1)
        curses.init_pair(C_ERR,    curses.COLOR_RED,     -1)
        curses.init_pair(C_FMT,    curses.COLOR_GREEN,   -1)
    except curses.error:
        pass
