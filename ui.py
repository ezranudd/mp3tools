"""
Shared UI foundation for the curses modules (tui, browse, sync_library,
import_preview).

This module is the single source of truth for the color palette so that every
screen uses the same pair numbers and colors. Previously browse.py owned pairs
1-7, tui.py added 8-11, and sync_library.py defined its own conflicting 1-6 set
that had to be re-initialized before every use. All of them now import from here.
"""

import curses

from termtext import fit_cells

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


# ── Chrome helpers ────────────────────────────────────────────────────────────
# One renderer for the top header bar and one for the bottom status/footer bar,
# so every screen shares the same colors, full-width fill, and corner-safe
# clipping (fit to w-1 to avoid the bottom-right cursor-advance error).

def header_bar(win, title: str, context: str = "") -> None:
    """Draw the top header: ' Title   context', white on blue, full width.

    Titles should be Title Case for consistency across screens.
    """
    try:
        _, w = win.getmaxyx()
        text = f" {title}" + (f"  {context}" if context else "")
        win.addstr(0, 0, fit_cells(text, w - 1), curses.color_pair(C_HDR) | curses.A_BOLD)
    except curses.error:
        pass


def keyhints(items: "list[tuple[str, str]]") -> str:
    """Format key hints as 'key=Action' joined by two spaces.

    Convention for the order of *items*: movement first, then actions, then
    back/quit last. Use 'Quit' only on the root menu, 'Back' for screens that
    pop to their parent, and 'Cancel' only when the key abandons an in-progress
    action.
    """
    return "  ".join(f"{k}={v}" for k, v in items)


def status_bar(win, text: str, attr: int | None = None) -> None:
    """Draw the bottom status/footer bar, black on cyan, full width.

    *text* is rendered with a single leading space; pass key hints built with
    keyhints(), or any transient message.
    """
    try:
        h, w = win.getmaxyx()
        win.addstr(h - 1, 0, fit_cells(" " + text, w - 1),
                   curses.color_pair(C_BAR) if attr is None else attr)
    except curses.error:
        pass


def confirm_key(win, prompt: str, *, default: bool = False) -> bool:
    """Single-key confirm drawn on the bottom bar. Returns True/False.

    `y` → True, `n`/Esc → False, Enter → *default*. The `[y/N]` / `[Y/n]`
    idiom (capital marks the default) is the standard confirm gesture across
    all screens; bulk/typed confirmations are intentionally not used.
    """
    status_bar(win, f"{prompt}  [{'Y/n' if default else 'y/N'}]")
    win.refresh()
    while True:
        try:
            key = win.getch()
        except KeyboardInterrupt:
            return False
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27):
            return False
        if key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            return default
