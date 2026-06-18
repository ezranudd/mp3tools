#!/usr/bin/env python3
"""Unified curses TUI entry point for MP3TOOLS."""

import curses
import os
import re
import shutil
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import settings as settings_mod
from termtext import cell_width, clip_cells, fit_cells

os.environ.setdefault("ESCDELAY", "25")

import browse as _browse_mod
from browse import (
    _put, _text_input, _choose,
    build_tree,
)
from ui import (
    C_HDR, C_BAR, C_DIM, C_ARTIST, C_ALBUM, C_TRACK, C_EDIT,
    C_SEL, C_OK, C_WARN, C_ERR,
    init_colors as _init_colors,
)


# ── App state ─────────────────────────────────────────────────────────────────

@dataclass
class AppState:
    library: Path | None = None
    dry_run: bool = True
    cfg: dict = field(default_factory=dict)

    def reload_cfg(self) -> None:
        if self.library:
            self.cfg = settings_mod.load(self.library)


# ── Navigation protocol ───────────────────────────────────────────────────────

class _PopAndPush:
    """Return from handle() to atomically replace the current view."""
    __slots__ = ("view",)
    def __init__(self, view: "View") -> None:
        self.view = view


class View:
    auto_pop: bool = False  # set True on views whose entire lifecycle runs inside draw()
    def draw(self, stdscr) -> None: ...
    def handle(self, key: int) -> "View | _PopAndPush | None":
        return None


# ── Output capture ────────────────────────────────────────────────────────────

class StreamingCapture:
    """Redirect stdout; each completed line is appended to a LogPane."""
    def __init__(self, log: "LogPane", render=None) -> None:
        self._log = log
        self._render = render
        self._linebuf = ""
        self._old = None

    def write(self, text: str) -> int:
        text = text.replace("\r", "\n")
        self._linebuf += text
        while "\n" in self._linebuf:
            line, self._linebuf = self._linebuf.split("\n", 1)
            self._log.append([line])
            if self._render:
                self._render()
        return len(text)

    def flush(self) -> None:
        pass

    def __enter__(self) -> "StreamingCapture":
        self._old = sys.stdout
        sys.stdout = self
        return self

    def __exit__(self, *_) -> None:
        if self._linebuf:
            self._log.append([self._linebuf])
            self._linebuf = ""
            if self._render:
                self._render()
        sys.stdout = self._old


# ── Scrollable log pane ───────────────────────────────────────────────────────

_MAX_LOG_LINES = 5000


class LogPane:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._scroll = 0
        self._follow = True
        self._lock = threading.RLock()

    def append(self, new_lines: list[str]) -> None:
        with self._lock:
            self.lines.extend(new_lines)
            if len(self.lines) > _MAX_LOG_LINES:
                self.lines = self.lines[-_MAX_LOG_LINES:]
            if self._follow:
                self._scroll = len(self.lines)  # clamped to len-view_h in draw()

    def update_last(self, line: str) -> None:
        """Replace the last line in place (for live progress updates)."""
        with self._lock:
            if self.lines:
                self.lines[-1] = line
            else:
                self.lines.append(line)

    def add_sep(self, text: str) -> None:
        self.append(["", f"  {'─' * 4}  {text}  {'─' * 4}", ""])

    def scroll(self, delta: int, view_h: int) -> None:
        with self._lock:
            self._follow = False
            cap = max(0, len(self.lines) - view_h)
            self._scroll = max(0, min(cap, self._scroll + delta))

    def draw(self, stdscr, top: int, bottom: int) -> None:
        h, w = stdscr.getmaxyx()
        view_h = bottom - top
        with self._lock:
            lines = list(self.lines)
            scroll = min(self._scroll, max(0, len(lines) - view_h))
        for i in range(view_h):
            idx = scroll + i
            row = top + i
            if row >= h:
                break
            if idx < len(lines):
                _put(stdscr, row, 0, clip_cells(lines[idx], w - 1))
            else:
                _put(stdscr, row, 0, " " * min(w - 1, 1))


@dataclass
class _UiRequest:
    kind: str
    prompt: str = ""
    options: list[tuple[str, str]] | None = None
    entries: object | None = None
    has_lossless: bool = False
    response: object | None = None
    event: threading.Event = field(default_factory=threading.Event)


class AsyncOperationView(View):
    """Run slow script-era work in a worker and service curses-only requests on the UI thread."""

    def _init_async(self) -> None:
        self.log = LogPane()
        self._done = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._request: _UiRequest | None = None
        self._progress = ""
        self._stdscr = None
        self._lock = threading.RLock()

    def _start_worker(self, target) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._thread = threading.Thread(target=self._worker_main, args=(target,), daemon=True)
        self._thread.start()

    def _worker_main(self, target) -> None:
        try:
            target()
        except BaseException as exc:
            self.log.append(["", f"ERROR: {exc}", traceback.format_exc().rstrip()])
        finally:
            with self._lock:
                self._progress = ""
                self._done = True

    def _request_ui(self, request: _UiRequest):
        with self._lock:
            self._request = request
        request.event.wait()
        with self._lock:
            if self._request is request:
                self._request = None
        return request.response

    def _request_text(self, prompt: str) -> str:
        result = self._request_ui(_UiRequest(kind="text", prompt=prompt))
        return str(result or "")

    def _request_choice(self, prompt: str) -> str:
        keys = re.findall(r'\[([A-Za-z0-9])\]', prompt)
        opts = []
        for k in keys:
            m = re.search(r'\[' + re.escape(k) + r'\]([\w ]*)', prompt, re.IGNORECASE)
            label = (k + (m.group(1).strip() if m else "")).strip()
            opts.append((k.lower(), label or k.upper()))
        if not opts:
            result = self._request_ui(_UiRequest(kind="text", prompt=prompt))
            return str(result or "").lower()[:1]
        result = self._request_ui(_UiRequest(kind="choice", prompt=prompt, options=opts))
        return str(result or opts[-1][0]).lower()[:1]

    def _request_preview(self, entries, has_lossless) -> bool:
        result = self._request_ui(_UiRequest(
            kind="preview",
            entries=entries,
            has_lossless=has_lossless,
        ))
        return bool(result)

    def _set_progress(self, text: str) -> None:
        with self._lock:
            self._progress = text

    def _status_detail(self) -> str:
        with self._lock:
            if self._request:
                if self._request.kind == "preview":
                    return " Preview needs input..."
                return " Input needed..."
            return self._progress

    def tick(self, stdscr):
        req = None
        with self._lock:
            req = self._request
        if req and not req.event.is_set():
            self._service_request(stdscr, req)
        return self

    def _service_request(self, stdscr, req: _UiRequest) -> None:
        self._render(stdscr)
        h, _ = stdscr.getmaxyx()
        if req.kind == "text":
            req.response = _text_input(stdscr, h - 1, req.prompt)
        elif req.kind == "choice":
            req.response = _choose(stdscr, h - 1, "", req.options or [])
        elif req.kind == "preview":
            from import_preview import run_preview_in_session
            stdscr.timeout(-1)
            try:
                _init_colors()
                req.response = run_preview_in_session(
                    stdscr,
                    req.entries,  # type: ignore[arg-type]
                    req.has_lossless,
                )
                _init_colors()
            finally:
                stdscr.timeout(100)
        req.event.set()


# ── Main menu ─────────────────────────────────────────────────────────────────

_MENU = [
    ("1", "Audit",          "Scan and report compliance issues  (read-only)"),
    ("2", "Browse",         "Browse library in an interactive tag editor"),
    ("3", "Standardize",    "Run all fixes; prompts for missing tags"),
    ("4", "Import",         "Copy and standardize tracks from another directory"),
    ("5", "Import from CD", "Rip a CD and import tracks into the library"),
    ("6", "Sync",           "Sync selected artists to a device"),
    ("7", "Settings",       "Configure library preferences"),
]

_TITLE_ART = [
    " __  __ ____ _____ _____ ___   ___  _     ____  ",
    "|  \\/  |  _ \\___ /_   _/ _ \\ / _ \\| |   / ___| ",
    "| |\\/| | |_) ||_ \\ | || | | | | | | |   \\___ \\ ",
    "| |  | |  __/___) || || |_| | |_| | |___ ___) |",
    "|_|  |_|_|  |____/ |_| \\___/ \\___/|_____|____/ ",
]


class MainMenuView(View):
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._flash = ""
        self._stdscr = None
        self._sel = 0  # highlighted menu item (0–5)

    def draw(self, stdscr) -> None:
        _init_colors()
        self._stdscr = stdscr
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        lib_str = str(self.state.library) if self.state.library else "(no library — press d)"
        mode_str = " DRY RUN " if self.state.dry_run else " LIVE "
        mode_attr = (curses.color_pair(C_WARN) if self.state.dry_run
                     else curses.color_pair(C_ERR)) | curses.A_BOLD
        mode_w = cell_width(mode_str)
        _put(stdscr, 0, 0, fit_cells(f" {lib_str}", w - mode_w),
             curses.color_pair(C_HDR) | curses.A_BOLD)
        _put(stdscr, 0, w - mode_w, mode_str, mode_attr)

        row = 2
        title_attr = curses.color_pair(C_ARTIST) | curses.A_BOLD
        for line in _TITLE_ART:
            if row >= h - 3:
                break
            _put(stdscr, row, 2, clip_cells(line, max(0, w - 3)), title_attr)
            row += 1
        row += 1
        for idx, (key, name, desc) in enumerate(_MENU):
            if row >= h - 3:
                break
            label = f"  [{key}] {name}"
            if idx == self._sel:
                _put(stdscr, row, 0, fit_cells(label, w - 1),
                     curses.A_REVERSE | curses.A_BOLD)
            else:
                _put(stdscr, row, 2, f"[{key}] {name}",
                     curses.color_pair(C_ARTIST) | curses.A_BOLD)
            if row + 1 < h - 2:
                _put(stdscr, row + 1, 6, desc, curses.color_pair(C_DIM) | curses.A_DIM)
            row += 2

        if row + 1 < h - 1:
            _put(stdscr, row + 1, 2, "j/k=Move  Enter=Launch  d=Directory  m=Mode  q=Quit",
                 curses.color_pair(C_DIM))

        bar = fit_cells(" " + self._flash if self._flash else "", w - 1)
        _put(stdscr, h - 1, 0, bar, curses.color_pair(C_BAR))
        stdscr.refresh()

    def handle(self, key: int) -> "View | None":
        self._flash = ""
        ch = chr(key).lower() if 0 < key < 256 else ""

        if ch == "q" or key == 27:
            return None

        # Cursor navigation
        if key in (curses.KEY_UP, ord("k")):
            self._sel = max(0, self._sel - 1)
            return self
        if key in (curses.KEY_DOWN, ord("j")):
            self._sel = min(len(_MENU) - 1, self._sel + 1)
            return self

        if ch == "d":
            return DirPickerView(self.state, purpose="library")
        if ch == "m":
            self.state.dry_run = not self.state.dry_run
            return self

        if not self.state.library:
            self._flash = "Press d to select a library directory first"
            return self

        # Enter / right arrow activates the highlighted item
        if key in (curses.KEY_ENTER, ord("\n"), ord("\r"), curses.KEY_RIGHT):
            ch = str(self._sel + 1)

        if ch == "5":
            return self._launch_cd_rip()
        _ACTIONS = {
            "1": AuditView, "2": BrowseView, "3": StandardizeView,
            "4": ImportSourceView, "6": DevicePickerView, "7": SettingsView,
        }
        if ch in _ACTIONS:
            return _ACTIONS[ch](self.state)

        return self

    def _launch_cd_rip(self) -> "View":
        import rip_cd as _rip_mod
        devices = _rip_mod.detect_cd_devices()
        if not devices:
            self._flash = "No CD drive found (checked /dev/cdrom, /dev/sr0 ...)"
            return self
        return RipCDView(self.state, device=devices[0])


# ── Directory picker ──────────────────────────────────────────────────────────

class DirPickerView(View):
    """Navigate the filesystem and select a directory."""

    def __init__(self, state: AppState, purpose: str = "library",
                 start: Path | None = None) -> None:
        self.state = state
        self.purpose = purpose
        lib = state.library
        self._cwd = (start or (lib if lib else Path.cwd())).resolve()
        self._entries: list[Path] = []
        self._sel = 0
        self._scroll = 0
        self._flash = ""
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._entries = sorted(
                d for d in self._cwd.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
        except PermissionError:
            self._entries = []
        self._sel = min(self._sel, max(0, len(self._entries) - 1))

    def _title(self) -> str:
        titles = {
            "library": " Select Library ",
            "source":  " Select Import Source ",
        }
        return titles.get(self.purpose, " Select Directory ")

    def draw(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        keys = " j/k=Move  →/Enter=Open  ←/u=Up  .=Use this dir  q=Cancel "
        gap = max(0, w - cell_width(keys))
        header = fit_cells(f" {self._cwd}", gap) + keys
        _put(stdscr, 0, 0, clip_cells(header, w),
             curses.color_pair(C_HDR) | curses.A_BOLD)

        list_h = max(1, h - 2)
        if self._sel < self._scroll:
            self._scroll = self._sel
        elif self._entries and self._sel >= self._scroll + list_h:
            self._scroll = self._sel - list_h + 1

        for i in range(list_h):
            idx = self._scroll + i
            row = i + 1
            if row >= h - 1:
                break
            if idx >= len(self._entries):
                _put(stdscr, row, 0, " ")
                continue
            name = "  " + self._entries[idx].name + "/"
            if idx == self._sel:
                _put(stdscr, row, 0, fit_cells(name, w - 1),
                     curses.A_REVERSE | curses.A_BOLD)
            else:
                _put(stdscr, row, 0, clip_cells(name, w - 1))

        if not self._entries:
            _put(stdscr, 2, 2, "(no subdirectories)",
                 curses.color_pair(C_DIM) | curses.A_DIM)

        bar = fit_cells(" " + self._flash if self._flash else "", w - 1)
        _put(stdscr, h - 1, 0, bar, curses.color_pair(C_BAR))
        stdscr.refresh()

    def handle(self, key: int) -> "View | _PopAndPush | None":
        self._flash = ""
        ch = chr(key).lower() if 0 < key < 256 else ""

        if key == 27 or ch == "q":
            return None

        if key in (curses.KEY_UP, ord("k")):
            self._sel = max(0, self._sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            if self._entries:
                self._sel = min(len(self._entries) - 1, self._sel + 1)
        elif key in (curses.KEY_HOME,):
            self._sel = 0
        elif key in (curses.KEY_END,):
            self._sel = max(0, len(self._entries) - 1)
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r"), curses.KEY_RIGHT):
            if self._entries:
                self._cwd = self._entries[self._sel]
                self._sel = 0
                self._scroll = 0
                self._refresh()
        elif key == curses.KEY_LEFT or ch == "u":
            parent = self._cwd.parent
            if parent != self._cwd:
                self._cwd = parent
                self._sel = 0
                self._scroll = 0
                self._refresh()
        elif ch == ".":
            return self._select(self._cwd)
        elif key == curses.KEY_PPAGE:
            pass  # page up not needed for dir picker

        return self

    def _select(self, path: Path) -> "View | _PopAndPush | None":
        if self.purpose == "library":
            self.state.library = path
            self.state.reload_cfg()
            return None
        return None  # subclasses override


# ── Import source picker ──────────────────────────────────────────────────────

class ImportSourceView(DirPickerView):
    def __init__(self, state: AppState) -> None:
        super().__init__(state, purpose="source", start=state.library)

    def _select(self, path: Path) -> "View | _PopAndPush | None":
        lib = self.state.library
        if lib and (path == lib or lib in path.parents):
            self._flash = "Source cannot be the same as or inside the library"
            return self
        return _PopAndPush(ImportView(self.state, source=path))


# ── Device picker ─────────────────────────────────────────────────────────────

def _mounted_devices() -> list[dict]:
    skip_fs = {
        "sysfs", "proc", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "pstore", "bpf", "autofs", "mqueue", "hugetlbfs", "debugfs", "tracefs",
        "fusectl", "configfs", "securityfs", "efivarfs", "overlay", "nsfs",
        "ramfs", "squashfs",
    }
    skip_prefixes = ("/sys", "/proc", "/dev", "/run")
    seen: set[Path] = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = Path(parts[1])
                if (parts[2] not in skip_fs
                        and mount != Path("/")
                        and not any(str(mount).startswith(p) for p in skip_prefixes)
                        and mount.is_dir() and mount not in seen):
                    seen.add(mount)
    except OSError:
        pass
    for base in (Path("/media"), Path("/mnt")):
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            subs = [s for s in item.iterdir() if s.is_dir() and not s.name.startswith(".")]
            for sub in (sorted(subs) if subs else [item]):
                seen.add(sub)
    devices = []
    for path in sorted(seen):
        try:
            usage = shutil.disk_usage(path)
            devices.append({"path": path, "free": usage.free, "total": usage.total})
        except OSError:
            devices.append({"path": path, "free": None, "total": None})
    return devices


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(size)
    for unit in units:
        if v < 1024 or unit == units[-1]:
            return f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return str(size)


class DevicePickerView(View):
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._devices = _mounted_devices()
        self._sel = 0
        self._flash = ""

    def draw(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _put(stdscr, 0, 0, fit_cells(" Select Device ", w),
             curses.color_pair(C_HDR) | curses.A_BOLD)

        row = 2
        if not self._devices:
            _put(stdscr, row, 2, "(no mounted devices found)",
                 curses.color_pair(C_DIM) | curses.A_DIM)
        else:
            for i, dev in enumerate(self._devices):
                if row + 1 >= h - 2:
                    break
                name = dev["path"].name or str(dev["path"])
                info = (f"{_fmt_size(dev['free'])} free / {_fmt_size(dev['total'])}"
                        if dev["free"] is not None else "size unknown")
                label = f"  {name}  "
                sub   = f"    {dev['path']}  {info}"
                attr = curses.A_REVERSE | curses.A_BOLD if i == self._sel else 0
                _put(stdscr, row, 0, fit_cells(label, w - 1), attr)
                _put(stdscr, row + 1, 0, clip_cells(sub, w - 1),
                     curses.color_pair(C_DIM) | curses.A_DIM)
                row += 2

        row += 1
        if row < h - 1:
            _put(stdscr, row, 2, "j/k=Move  Enter=Select  b=Browse  q=Cancel",
                 curses.color_pair(C_DIM))
        bar = fit_cells(" " + self._flash if self._flash else "", w - 1)
        _put(stdscr, h - 1, 0, bar, curses.color_pair(C_BAR))
        stdscr.refresh()

    def handle(self, key: int) -> "View | _PopAndPush | None":
        self._flash = ""
        ch = chr(key).lower() if 0 < key < 256 else ""

        if key == 27 or ch == "q":
            return None
        if key in (curses.KEY_UP, ord("k")) and self._devices:
            self._sel = max(0, self._sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")) and self._devices:
            self._sel = min(len(self._devices) - 1, self._sel + 1)
        elif ch == "b":
            return _BrowseForDevice(self.state)
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")) and self._devices:
            return self._select(self._devices[self._sel]["path"])

        return self

    def _select(self, path: Path) -> "_PopAndPush | None":
        lib = self.state.library
        if lib and path == lib:
            self._flash = "Device and library cannot be the same directory"
            return self
        return _PopAndPush(SyncView(self.state, device=path))


class _BrowseForDevice(DirPickerView):
    """DirPicker that, on selection, launches SyncView."""
    def __init__(self, state: AppState) -> None:
        super().__init__(state, purpose="device")

    def _select(self, path: Path) -> "View | _PopAndPush | None":
        lib = self.state.library
        if lib and path == lib:
            self._flash = "Device and library cannot be the same directory"
            return self
        return _PopAndPush(SyncView(self.state, device=path))


# ── Browse view ───────────────────────────────────────────────────────────────

class BrowseView(View):
    auto_pop = True

    def __init__(self, state: AppState) -> None:
        self.state = state

    def draw(self, stdscr) -> None:
        from browse import run_in_session
        stdscr.timeout(-1)
        try:
            run_in_session(stdscr, self.state.library)
            _init_colors()  # Re-init our colors after browse resets them
        finally:
            stdscr.timeout(100)


# ── Sync view ─────────────────────────────────────────────────────────────────

class SyncView(View):
    auto_pop = True

    def __init__(self, state: AppState, device: Path) -> None:
        self.state = state
        self.device = device

    def draw(self, stdscr) -> None:
        from sync_library import run_in_session
        stdscr.timeout(-1)
        try:
            run_in_session(stdscr, self.state.library, self.device, self.state.dry_run)
            _init_colors()
        finally:
            stdscr.timeout(100)


# ── Audit view ────────────────────────────────────────────────────────────────

class AuditView(AsyncOperationView):
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._init_async()

    def draw(self, stdscr) -> None:
        self._stdscr = stdscr
        self._start_worker(self._run)
        self._render(stdscr)

    def _render(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        hdr = fit_cells(f" Audit  {self.state.library}", w)
        _put(stdscr, 0, 0, hdr, curses.color_pair(C_HDR) | curses.A_BOLD)
        self.log.draw(stdscr, 1, h - 1)
        status = " j/k=Scroll  q=Back" if self._done else " Scanning..."
        _put(stdscr, h - 1, 0, fit_cells(status, w - 1), curses.color_pair(C_BAR))
        stdscr.refresh()

    def _run(self) -> None:
        import audit
        root = self.state.library
        with StreamingCapture(self.log):
            results = audit.scan(root)
        with StreamingCapture(self.log):
            audit.print_report(results, root, show_ok=False)

    def handle(self, key: int) -> "View | None":
        if not self._done:
            return self
        h = self._stdscr.getmaxyx()[0] if self._stdscr else 24
        ch = chr(key).lower() if 0 < key < 256 else ""
        if ch == "q" or key == 27:
            return None
        if key in (curses.KEY_UP, ord("k")):
            self.log.scroll(-1, h - 2)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.log.scroll(1, h - 2)
        elif key == curses.KEY_PPAGE:
            self.log.scroll(-(h - 2), h - 2)
        elif key == curses.KEY_NPAGE:
            self.log.scroll(h - 2, h - 2)
        return self


# ── Standardize view ──────────────────────────────────────────────────────────

_STD_STEP_NAMES = [
    "Merge disc subfolders",
    "Fix missing tags",
    "Enforce ID3v2.3",
    "Strip extraneous tags",
    "Normalize special characters",
    "Normalize years",
    "Renumber tracks",
    "Zero-pad track numbers",
    "Set total track counts",
    "Rename album folders",
    "Deduplicate albums",
    "Rename artist folders",
    "Rename MP3 files",
    "Clean files",
]


class StandardizeView(AsyncOperationView):
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._step_num = 0
        self._step_name = "Starting..."
        self._total = 15
        self._init_async()

    def draw(self, stdscr) -> None:
        self._stdscr = stdscr
        self._start_worker(self._execute)
        self._render(stdscr)

    def _render(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        mode = " DRY RUN " if self.state.dry_run else " LIVE "
        mode_attr = (curses.color_pair(C_WARN) if self.state.dry_run
                     else curses.color_pair(C_ERR)) | curses.A_BOLD
        step_s = f" {self._step_num}/{self._total} " if self._step_num else ""
        step_w = cell_width(step_s) + cell_width(mode)
        _put(stdscr, 0, 0, fit_cells(f" Standardize  {self.state.library}", w - step_w),
             curses.color_pair(C_HDR) | curses.A_BOLD)
        _put(stdscr, 0, w - step_w, step_s, curses.color_pair(C_HDR) | curses.A_BOLD)
        _put(stdscr, 0, w - cell_width(mode), mode, mode_attr)
        self.log.draw(stdscr, 1, h - 1)
        detail = self._status_detail()
        if self._done:
            bar = " j/k=Scroll  q=Back"
        elif detail:
            bar = detail
        else:
            bar = f" Step {self._step_num}: {self._step_name}..."
        _put(stdscr, h - 1, 0, fit_cells(bar, w - 1), curses.color_pair(C_BAR))
        stdscr.refresh()

    def _set_step(self, num: int, name: str) -> None:
        self._step_num = num
        self._step_name = name

    def _make_ask_text(self):
        def ask(prompt: str) -> str:
            return self._request_text(prompt)
        return ask

    def _make_ask_choice(self):
        def ask(prompt: str) -> str:
            return self._request_choice(prompt)
        return ask

    def _execute(self) -> None:
        import standardize as std_mod
        from convert_lossless import step_convert_lossless

        root = self.state.library
        dry_run = self.state.dry_run
        cfg = self.state.cfg
        cover_art        = cfg.get("cover_art", "folder")
        cover_art_size   = cfg.get("cover_art_embed_size", 500)
        fetch_art_online     = cfg.get("fetch_art_online", False)
        enforce_artist       = cfg.get("enforce_artist_equals_album_artist", False)
        replace_brackets     = cfg.get("replace_brackets_with_parentheses", False)
        preserve_replay_gain = cfg.get("preserve_replay_gain", False)
        preserve_tcmp         = cfg.get("preserve_tcmp", False)
        preserve_disc_numbers = cfg.get("preserve_disc_numbers", False)
        keep_apic             = cover_art in ("embed", "both")

        ask_text   = self._make_ask_text()
        ask_choice = self._make_ask_choice()

        with StreamingCapture(self.log):
            print(f"Directory : {root}")
            print(f"Cover art : {cover_art}")
            print(f"Mode      : {'DRY RUN' if dry_run else 'LIVE'}")

        # Step 0: lossless conversion
        self._set_step(0, "Convert lossless files")
        self.log.add_sep("Step 0: Convert lossless files")
        with StreamingCapture(self.log):
            step_convert_lossless(root, dry_run, ask_choice=ask_choice)

        # Steps 1–13
        for idx, fn in enumerate(std_mod.STEPS, 1):
            step_name = (_STD_STEP_NAMES[idx - 1]
                         if idx <= len(_STD_STEP_NAMES) else fn.__name__)
            self._set_step(idx, step_name)
            self.log.add_sep(f"Step {idx}: {self._step_name}")
            with StreamingCapture(self.log):
                if fn is std_mod.step_strip_tags:
                    fn(root, dry_run, keep_apic=keep_apic,
                       keep_replay_gain=preserve_replay_gain,
                       keep_tcmp=preserve_tcmp,
                       keep_tpos=preserve_disc_numbers)
                elif fn is std_mod.step_merge_subfolders:
                    fn(root, dry_run, preserve_tpos=preserve_disc_numbers)
                elif fn is std_mod.step_renumber_tracks:
                    fn(root, dry_run, respect_tpos=preserve_disc_numbers)
                elif fn is std_mod.step_pad_tracks:
                    fn(root, dry_run, respect_tpos=preserve_disc_numbers)
                elif fn is std_mod.step_set_total_tracks:
                    fn(root, dry_run, respect_tpos=preserve_disc_numbers)
                elif fn is std_mod.step_normalize_year and replace_brackets:
                    std_mod.step_replace_title_brackets(root, dry_run)
                    fn(root, dry_run)
                elif fn is std_mod.step_clean_files:
                    fn(root, dry_run, cover_art=cover_art, ask_choice=ask_choice)
                elif fn is std_mod.step_rename_files and enforce_artist:
                    std_mod.step_enforce_track_artist(root, dry_run)
                    fn(root, dry_run)
                elif fn is std_mod.step_fix_missing_tags:
                    fn(root, dry_run, ask_text=ask_text)
                elif fn is std_mod.step_rename_artist_folders:
                    fn(root, dry_run, ask_choice=ask_choice)
                else:
                    fn(root, dry_run)

        # Step 15: embed art
        if cover_art in ("embed", "both"):
            self._set_step(15, "Embed cover art")
            self.log.add_sep("Step 15: Embed cover art")
            with StreamingCapture(self.log):
                std_mod.step_embed_cover_art(
                    root, dry_run,
                    max_size=cover_art_size,
                    delete_covers=(cover_art == "embed"),
                )

        # Step 16: fetch art
        if fetch_art_online:
            self._set_step(16, "Fetch missing album art online")
            self.log.add_sep("Step 16: Fetch missing album art online")
            with StreamingCapture(self.log):
                std_mod.step_fetch_missing_art(
                    root, dry_run,
                    settings=self.state.cfg,
                    cover_art=cover_art,
                    max_size=cover_art_size,
                )
    def handle(self, key: int) -> "View | None":
        if not self._done:
            return self
        h = self._stdscr.getmaxyx()[0] if self._stdscr else 24
        ch = chr(key).lower() if 0 < key < 256 else ""
        if ch == "q" or key == 27:
            return None
        if key in (curses.KEY_UP, ord("k")):
            self.log.scroll(-1, h - 2)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.log.scroll(1, h - 2)
        elif key == curses.KEY_PPAGE:
            self.log.scroll(-(h - 2), h - 2)
        elif key == curses.KEY_NPAGE:
            self.log.scroll(h - 2, h - 2)
        return self


# ── Import view ───────────────────────────────────────────────────────────────

class ImportView(AsyncOperationView):
    def __init__(self, state: AppState, source: Path) -> None:
        self.state = state
        self.source = source
        self._init_async()

    def draw(self, stdscr) -> None:
        self._stdscr = stdscr
        self._start_worker(self._run)
        self._render(stdscr)

    def _render(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        mode = " DRY RUN " if self.state.dry_run else " LIVE "
        mode_attr = (curses.color_pair(C_WARN) if self.state.dry_run
                     else curses.color_pair(C_ERR)) | curses.A_BOLD
        mode_w = cell_width(mode)
        _put(stdscr, 0, 0, fit_cells(f" Import  {self.source}", w - mode_w),
             curses.color_pair(C_HDR) | curses.A_BOLD)
        _put(stdscr, 0, w - mode_w, mode, mode_attr)
        self.log.draw(stdscr, 1, h - 1)
        detail = self._status_detail()
        if self._done:
            status = " j/k=Scroll  q=Back"
        else:
            status = detail or " Importing..."
        _put(stdscr, h - 1, 0, fit_cells(status, w - 1), curses.color_pair(C_BAR))
        stdscr.refresh()

    def _make_ask_text(self):
        def ask(prompt: str) -> str:
            return self._request_text(prompt)
        return ask

    def _make_preview_fn(self):
        def preview(entries, has_lossless):
            return self._request_preview(entries, has_lossless)
        return preview

    def _make_ask_choice(self):
        def ask(prompt: str) -> str:
            return self._request_choice(prompt)
        return ask

    def _make_progress_fn(self):
        def progress(label: str, percent: int | None = None, done: bool = False) -> None:
            if done:
                self._set_progress("")
                return
            suffix = f" {percent:3d}%" if percent is not None else ""
            self._set_progress(f" Converting {label}{suffix}")
        return progress

    def _run(self) -> None:
        from import_tracks import import_tracks

        root = self.state.library
        dry_run = self.state.dry_run
        cfg = self.state.cfg

        ask_text    = self._make_ask_text()
        ask_choice  = self._make_ask_choice()
        preview_fn  = self._make_preview_fn()
        progress_fn = self._make_progress_fn()

        with StreamingCapture(self.log):
            import_tracks(
                self.source, root, dry_run,
                cover_art=cfg.get("cover_art", "folder"),
                cover_art_size=cfg.get("cover_art_embed_size", 500),
                settings=cfg,
                preview_fn=preview_fn,
                ask_text=ask_text,
                ask_choice=ask_choice,
                progress=progress_fn,
            )

    def handle(self, key: int) -> "View | None":
        if not self._done:
            return self
        h = self._stdscr.getmaxyx()[0] if self._stdscr else 24
        ch = chr(key).lower() if 0 < key < 256 else ""
        if ch == "q" or key == 27:
            return None
        if key in (curses.KEY_UP, ord("k")):
            self.log.scroll(-1, h - 2)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.log.scroll(1, h - 2)
        elif key == curses.KEY_PPAGE:
            self.log.scroll(-(h - 2), h - 2)
        elif key == curses.KEY_NPAGE:
            self.log.scroll(h - 2, h - 2)
        return self


# ── CD rip view ───────────────────────────────────────────────────────────────

class _CDImportView(ImportView):
    """ImportView subclass that deletes the (temp) source directory and optionally ejects the disc."""
    def __init__(self, state: AppState, source: Path, device: Path | None = None) -> None:
        super().__init__(state, source)
        self._cd_device = device

    def _run(self) -> None:
        try:
            super()._run()
        finally:
            shutil.rmtree(str(self.source), ignore_errors=True)
            if self._cd_device and self.state.cfg.get("eject_cd_after_import"):
                import rip_cd as _rip_mod
                _rip_mod.eject_device(self._cd_device)


class RipCDView(AsyncOperationView):
    def __init__(self, state: AppState, device: Path) -> None:
        self.state = state
        self.device = device
        self._rip_dir: Path | None = None
        self._rip_ok = False
        self._cancelling = False
        self._cancel_event = threading.Event()
        self._init_async()

    def draw(self, stdscr) -> None:
        self._stdscr = stdscr
        self._start_worker(self._run)
        self._render(stdscr)

    def _render(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _put(stdscr, 0, 0, fit_cells(f" Import from CD  {self.device}", w),
             curses.color_pair(C_HDR) | curses.A_BOLD)
        self.log.draw(stdscr, 1, h - 1)

        if self._done:
            if self._cancelling:
                status = " Cancelled — q=Back"
            elif self._rip_ok:
                status = " Rip complete — Enter=Import  q=Back"
            else:
                status = " Rip failed — q=Back"
        elif self._cancelling:
            status = " Cancelling..."
        else:
            status = " Ripping...  q=Cancel"

        _put(stdscr, h - 1, 0, fit_cells(status, w - 1), curses.color_pair(C_BAR))
        stdscr.refresh()

    def _run(self) -> None:
        import tempfile
        import rip_cd as _rip_mod
        self._rip_dir = Path(tempfile.mkdtemp(prefix="mp3tools_rip_"))

        def log(msg: str) -> None:
            self.log.append([msg])

        _bar_w = 24

        def progress(track: int, total: int, pct: int) -> None:
            if pct <= 0:
                return
            filled = int(_bar_w * pct / 100)
            bar = "█" * filled + "░" * (_bar_w - filled)
            self.log.update_last(f"  Ripping track {track}/{total}... [{bar}] {pct:3d}%")

        self._rip_ok = _rip_mod.rip(
            self.device, self._rip_dir,
            log_fn=log,
            progress_fn=progress,
            cancel_event=self._cancel_event,
        )

    def tick(self, stdscr) -> "View | None":
        result = super().tick(stdscr)
        if self._done and self._cancelling:
            if self._rip_dir:
                shutil.rmtree(str(self._rip_dir), ignore_errors=True)
                self._rip_dir = None
            return None
        return result

    def handle(self, key: int) -> "View | _PopAndPush | None":
        h = self._stdscr.getmaxyx()[0] if self._stdscr else 24
        ch = chr(key).lower() if 0 < key < 256 else ""

        # Cancel is available any time ripping is in progress
        if not self._done:
            if ch == "q" or key == 27:
                self._cancelling = True
                self._cancel_event.set()
            return self

        if ch == "q" or key == 27:
            if self._rip_dir:
                shutil.rmtree(str(self._rip_dir), ignore_errors=True)
            return None
        if key in (curses.KEY_UP, ord("k")):
            self.log.scroll(-1, h - 2)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.log.scroll(1, h - 2)
        elif key == curses.KEY_PPAGE:
            self.log.scroll(-(h - 2), h - 2)
        elif key == curses.KEY_NPAGE:
            self.log.scroll(h - 2, h - 2)
        elif (self._rip_ok and self._rip_dir
              and key in (curses.KEY_ENTER, ord("\n"), ord("\r"))):
            rip_dir = self._rip_dir
            self._rip_dir = None  # prevent double-cleanup in _CDImportView
            return _PopAndPush(_CDImportView(self.state, source=rip_dir, device=self.device))
        return self


# ── Settings view ─────────────────────────────────────────────────────────────

_COVER_ART_DESCS = {
    "folder": "Keep cover.jpg per album folder",
    "embed":  "Embed art in every track's ID3 tags",
    "both":   "Embed and keep cover.jpg",
}
_ART_SOURCE_KEYS = ("itunes", "musicbrainz", "theaudiodb", "discogs")
_ART_SOURCE_DESCS = {
    "itunes":      "Apple iTunes artwork search",
    "musicbrainz": "MusicBrainz + Cover Art Archive",
    "theaudiodb":  "TheAudioDB fallback (API key required)",
    "discogs":     "Discogs candidates in Browse only",
}


class SettingsView(View):
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.cfg = dict(state.cfg)
        self._flash = ""
        self._stdscr = None
        self._scroll = 0
        self._sel = self._first_interactive()

    def _first_interactive(self) -> int:
        for i, (k, _, _) in enumerate(self._lines()):
            if k:
                return i
        return 0

    def _next_interactive(self, rows: list, from_idx: int, direction: int) -> int:
        i = from_idx + direction
        while 0 <= i < len(rows):
            if rows[i][0]:
                return i
            i += direction
        return from_idx

    def _lines(self) -> list[tuple[str, str, int]]:
        """Return (key, text, attr) tuples for all setting lines."""
        cfg = self.cfg
        ca = cfg.get("cover_art", "folder")
        ca_size = cfg.get("cover_art_embed_size", 500)
        sources = cfg.get("art_sources", {})

        def on(v): return ("ON ", curses.color_pair(C_OK) | curses.A_BOLD) if v else ("OFF", curses.color_pair(C_DIM))

        rows = []
        rows.append(("",    "  Cover Art", curses.color_pair(C_ARTIST) | curses.A_BOLD))
        for i, key in enumerate(("folder", "embed", "both"), 1):
            mark = "* " if key == ca else "  "
            attr = curses.color_pair(C_OK) | curses.A_BOLD if key == ca else 0
            rows.append((str(i), f"  {mark}{key:<8}  {_COVER_ART_DESCS[key]}", attr))
        rows.append(("",    "", 0))
        rows.append(("4",   f"  Max embed size: {ca_size} px  (0 = no resize)",
                     curses.color_pair(C_ALBUM)))
        rows.append(("",    "", 0))

        v, a = on(cfg.get("enforce_artist_equals_album_artist", False))
        rows.append(("t",   f"  Enforce Artist = Album Artist  [{v}]  "
                            f"(rewrites each track artist from album artist tag)", a))

        v, a = on(cfg.get("replace_brackets_with_parentheses", False))
        rows.append(("b",   f"  Replace [] with () in titles  [{v}]", a))

        v, a = on(cfg.get("preserve_replay_gain", False))
        rows.append(("r",   f"  Preserve replay gain tags  [{v}]  "
                            f"(keeps TXXX:REPLAYGAIN_* during tag strip)", a))

        v, a = on(cfg.get("preserve_tcmp", False))
        rows.append(("i",   f"  Preserve iTunes compilation flag  [{v}]  "
                            f"(keeps/sets TCMP=1 when Artist ≠ Album Artist)", a))

        v, a = on(cfg.get("preserve_disc_numbers", False))
        rows.append(("n",   f"  Preserve disc numbers on merge  [{v}]  "
                            f"(writes TPOS; keeps per-disc TRCK instead of renumbering)", a))

        rows.append(("",    "", 0))
        rows.append(("",    "  CD Import", curses.color_pair(C_ARTIST) | curses.A_BOLD))

        v, a = on(cfg.get("eject_cd_after_import", False))
        rows.append(("e",   f"  Eject disc after import  [{v}]", a))

        rows.append(("",    "", 0))
        rows.append(("",    "  Online Art Fetch", curses.color_pair(C_ARTIST) | curses.A_BOLD))

        v, a = on(cfg.get("fetch_art_online", False))
        rows.append(("5",   f"  Fetch missing art during Standardize  [{v}]", a))
        rows.append(("",    "", 0))
        rows.append(("",    "  Artwork Sources", curses.color_pair(C_ARTIST) | curses.A_BOLD))

        for i, src in enumerate(_ART_SOURCE_KEYS, 6):
            v, a = on(sources.get(src, False))
            rows.append((str(i), f"  {src:<11}  [{v}]  {_ART_SOURCE_DESCS[src]}", a))

        rows.append(("",    "", 0))
        adb = "set" if cfg.get("theaudiodb_api_key") else "not set"
        dgs = "set" if cfg.get("discogs_token") else "not set"
        rows.append(("a",   f"  Set TheAudioDB API key  [{adb}]",
                     curses.color_pair(C_ALBUM)))
        rows.append(("d",   f"  Set Discogs token  [{dgs}]",
                     curses.color_pair(C_ALBUM)))
        rows.append(("",    "", 0))
        rows.append(("s",   "  Save and return", curses.color_pair(C_OK) | curses.A_BOLD))
        rows.append(("c",   "  Cancel", curses.color_pair(C_ERR) | curses.A_BOLD))
        return rows

    def draw(self, stdscr) -> None:
        self._stdscr = stdscr
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _put(stdscr, 0, 0, fit_cells(f" Settings  {self.state.library}", w),
             curses.color_pair(C_HDR) | curses.A_BOLD)

        rows = self._lines()
        list_h = h - 2

        # Keep sel visible
        max_scroll = max(0, len(rows) - list_h)
        if self._sel < self._scroll:
            self._scroll = self._sel
        elif self._sel >= self._scroll + list_h:
            self._scroll = self._sel - list_h + 1
        self._scroll = min(self._scroll, max_scroll)

        for i in range(list_h):
            idx = self._scroll + i
            row = i + 1
            if row >= h - 1 or idx >= len(rows):
                break
            row_key, text, attr = rows[idx]
            if idx == self._sel and row_key:
                _put(stdscr, row, 0, fit_cells(text, w - 1),
                     curses.A_REVERSE | curses.A_BOLD)
            else:
                _put(stdscr, row, 0, clip_cells(text, w - 1), attr)

        bar_hint = " j/k=Move  Enter=Select  Esc=Cancel"
        bar = fit_cells(" " + self._flash if self._flash else bar_hint, w - 1)
        _put(stdscr, h - 1, 0, bar, curses.color_pair(C_BAR))
        stdscr.refresh()

    def handle(self, key: int) -> "View | None":
        self._flash = ""
        ch = chr(key).lower() if 0 < key < 256 else ""

        # Cursor navigation
        if key in (curses.KEY_UP, ord("k")):
            rows = self._lines()
            self._sel = self._next_interactive(rows, self._sel, -1)
            return self
        if key in (curses.KEY_DOWN, ord("j")):
            rows = self._lines()
            self._sel = self._next_interactive(rows, self._sel, 1)
            return self

        # Escape = cancel
        if key == 27:
            return None

        # Enter / Space activates the highlighted row
        if key in (curses.KEY_ENTER, ord("\n"), ord("\r"), ord(" ")):
            rows = self._lines()
            sel_key = rows[self._sel][0] if self._sel < len(rows) else ""
            return self._activate(sel_key)

        return self

    def _activate(self, key: str) -> "View | None":
        self._flash = ""
        if not key:
            return self

        if key == "c":
            return None
        if key == "s":
            settings_mod.save(self.state.library, self.cfg)
            self.state.cfg = settings_mod.load(self.state.library)
            return None

        if key in ("1", "2", "3"):
            self.cfg["cover_art"] = ("folder", "embed", "both")[int(key) - 1]
        elif key == "4":
            self._prompt_text("Max embed size in px (0 = no resize): ",
                              str(self.cfg.get("cover_art_embed_size", 500)),
                              "cover_art_embed_size", int)
        elif key == "5":
            self.cfg["fetch_art_online"] = not self.cfg.get("fetch_art_online", False)
        elif key in ("6", "7", "8", "9"):
            src = _ART_SOURCE_KEYS[int(key) - 6]
            self.cfg.setdefault("art_sources", {})
            self.cfg["art_sources"][src] = not self.cfg["art_sources"].get(src, False)
        elif key == "t":
            self.cfg["enforce_artist_equals_album_artist"] = not self.cfg.get(
                "enforce_artist_equals_album_artist", False)
        elif key == "b":
            self.cfg["replace_brackets_with_parentheses"] = not self.cfg.get(
                "replace_brackets_with_parentheses", False)
        elif key == "r":
            self.cfg["preserve_replay_gain"] = not self.cfg.get(
                "preserve_replay_gain", False)
        elif key == "i":
            self.cfg["preserve_tcmp"] = not self.cfg.get("preserve_tcmp", False)
        elif key == "n":
            self.cfg["preserve_disc_numbers"] = not self.cfg.get(
                "preserve_disc_numbers", False)
        elif key == "e":
            self.cfg["eject_cd_after_import"] = not self.cfg.get(
                "eject_cd_after_import", False)
        elif key == "a":
            self._prompt_text("TheAudioDB API key (blank to clear): ",
                              self.cfg.get("theaudiodb_api_key", ""),
                              "theaudiodb_api_key")
        elif key == "d":
            self._prompt_text("Discogs token (blank to clear): ",
                              self.cfg.get("discogs_token", ""),
                              "discogs_token")

        # Re-sync _sel in case cfg changes shifted which row should be highlighted
        rows = self._lines()
        if self._sel >= len(rows) or not rows[self._sel][0]:
            self._sel = self._first_interactive()

        return self

    def _prompt_text(self, prompt: str, prefill: str, cfg_key: str,
                     cast=None) -> None:
        if not self._stdscr:
            return
        h, _ = self._stdscr.getmaxyx()
        result = _text_input(self._stdscr, h - 1, prompt, prefill)
        if result is None:
            return
        if cast:
            try:
                result = cast(result)
            except (ValueError, TypeError):
                self._flash = "Invalid value"
                return
        self.cfg[cfg_key] = result


# ── Main loop ─────────────────────────────────────────────────────────────────

def _run(stdscr, state: AppState) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(100)
    _init_colors()

    def apply_result(result, current: View) -> None:
        if result is None:
            stack.pop()
        elif isinstance(result, _PopAndPush):
            stack[-1] = result.view
        elif result is not current:
            stack.append(result)
        # else: result is current -> just redraw

    stack: list[View] = [MainMenuView(state)]
    while stack:
        v = stack[-1]
        v.draw(stdscr)
        if v.auto_pop:
            stack.pop()
            continue
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            break
        if key == -1:
            tick = getattr(v, "tick", None)
            if tick:
                apply_result(tick(stdscr), v)
            continue
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            continue
        result = v.handle(key)
        apply_result(result, v)


def main() -> None:
    if sys.version_info < (3, 10):
        print(f"Error: Python 3.10+ required (found {sys.version})", file=sys.stderr)
        sys.exit(1)

    state = AppState(library=Path.cwd().resolve())
    state.reload_cfg()

    try:
        curses.wrapper(_run, state)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
