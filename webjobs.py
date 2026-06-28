"""
Web job layer — a headless port of the TUI's AsyncOperationView / _UiRequest
model (tui.py:150-265) so interactive operations run over HTTP.

A job runs the same business-logic functions the TUI uses, on a background
thread. The injected ask_text / ask_choice / preview_fn callbacks store a
pending prompt on the job and block on a threading.Event; the HTTP layer polls
the job, sees state == "waiting", collects the answer from the browser, and
respond() unblocks the worker. Captured stdout is appended to the job's log.

Because stdout redirection is process-global, only ONE job runs at a time
(matches the TUI, which runs one operation at a time).
"""
from __future__ import annotations

import re
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import settings as settings_mod

_MAX_LOG_LINES = 5000


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _eta(done_bytes: int, total_bytes: int, elapsed: float) -> str:
    """' · ETA M:SS' from linear extrapolation, or '' when not meaningful yet."""
    if done_bytes <= 0 or done_bytes >= total_bytes or elapsed <= 0.5:
        return ""
    remaining = (total_bytes - done_bytes) * elapsed / done_bytes
    return " · ETA " + _fmt_duration(remaining)


@dataclass
class _Prompt:
    """A pending request for user input (mirrors tui._UiRequest)."""
    kind: str                                   # "text" | "choice" | "preview"
    prompt: str = ""
    options: list[dict] | None = None           # [{key,label}, ...] for choice
    entries: list | None = None                 # serialized rows for preview
    has_lossless: bool = False
    default_bitrate: int = 320                   # preview: default lossless→MP3 bitrate
    event: threading.Event = field(default_factory=threading.Event)
    response: object | None = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "prompt": self.prompt,
            "options": self.options or [],
            "entries": self.entries or [],
            "has_lossless": self.has_lossless,
            "default_bitrate": self.default_bitrate,
        }


class Job:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = "running"                  # running | waiting | done | error
        self.log: list[str] = []
        self.progress = ""
        self.percent: int | None = None          # 0-100 for a determinate bar, else None
        self.result: dict = {}
        self.error = ""
        self.prompt: _Prompt | None = None
        self.cancelled = False
        self._import_started: float | None = None   # monotonic clock for import ETA
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    # ── log / progress ────────────────────────────────────────────────────────
    def append_log(self, text: str) -> None:
        with self._lock:
            for line in text.split("\n"):
                self.log.append(line)
            if len(self.log) > _MAX_LOG_LINES:
                self.log = self.log[-_MAX_LOG_LINES:]

    def add_sep(self, text: str) -> None:
        self.append_log(f"\n  ──── {text} ────")

    def set_progress(self, text: str, percent: int | None = None) -> None:
        with self._lock:
            self.progress = text
            self.percent = percent

    # ── interactive prompt round-trip (the heart) ─────────────────────────────
    def _ask(self, prompt: _Prompt):
        with self._lock:
            self.prompt = prompt
            self.state = "waiting"
        prompt.event.wait()                     # blocks the worker thread
        with self._lock:
            self.prompt = None
            if self.state == "waiting":
                self.state = "running"
        return prompt.response

    def ask_text(self, prompt: str) -> str:
        if self.cancelled:
            return ""
        return str(self._ask(_Prompt(kind="text", prompt=prompt)) or "")

    def ask_choice(self, prompt: str) -> str:
        if self.cancelled:
            return ""
        # Parse [X]label tokens out of the prompt to build buttons (tui:204-215).
        keys = re.findall(r"\[([A-Za-z0-9])\]", prompt)
        opts: list[dict] = []
        for k in keys:
            m = re.search(r"\[" + re.escape(k) + r"\]([\w ]*)", prompt, re.IGNORECASE)
            label = (k + (m.group(1).strip() if m else "")).strip()
            opts.append({"key": k.lower(), "label": label or k.upper()})
        if not opts:
            return str(self._ask(_Prompt(kind="text", prompt=prompt)) or "").lower()[:1]
        res = self._ask(_Prompt(kind="choice", prompt=prompt, options=opts))
        return str(res or opts[-1]["key"]).lower()[:1]

    def preview_fn(self, entries, has_lossless) -> bool:
        if self.cancelled:
            return False
        rows = _serialize_entries(entries, getattr(self, "library_root", None))
        res = self._ask(_Prompt(kind="preview", entries=rows, has_lossless=bool(has_lossless),
                                default_bitrate=getattr(self, "import_bitrate", 320)))
        if not isinstance(res, dict):
            return bool(res)
        _apply_entry_edits(entries, res.get("entries"))
        return bool(res.get("proceed"))

    def import_progress(self, done: int, total: int, fraction: float) -> None:
        """Whole-import progress for the determinate bar + a header ETA.
        `fraction` (0-1) includes the in-flight track's conversion progress."""
        now = time.monotonic()
        if self._import_started is None:
            self._import_started = now
        eta = _eta(fraction, 1.0, now - self._import_started)
        pct = int(min(1.0, max(0.0, fraction)) * 100)
        self.set_progress(f"{done}/{total}{eta}", percent=pct)

    # ── control / serialization ───────────────────────────────────────────────
    def respond(self, value) -> None:
        with self._lock:
            p = self.prompt
        if p is not None and not p.event.is_set():
            p.response = value
            p.event.set()

    def cancel(self) -> None:
        self.cancelled = True
        with self._lock:
            p = self.prompt
        if p is not None and not p.event.is_set():
            p.response = "" if p.kind != "preview" else {"proceed": False}
            p.event.set()

    def to_json(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "log": list(self.log),
                "progress": self.progress,
                "percent": self.percent,
                "prompt": self.prompt.to_json() if self.prompt else None,
                "result": self.result,
                "error": self.error,
            }


# ── stdout capture (mirrors tui.StreamingCapture) ─────────────────────────────

class _Capture:
    def __init__(self, job: Job) -> None:
        self._job = job
        self._buf = ""
        self._old = None

    def write(self, text: str) -> int:
        text = text.replace("\r", "\n")
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._job.append_log(line)
        return len(text)

    def flush(self) -> None:
        pass

    def __enter__(self) -> "_Capture":
        self._old = sys.stdout
        sys.stdout = self
        return self

    def __exit__(self, *_) -> None:
        if self._buf:
            self._job.append_log(self._buf)
            self._buf = ""
        sys.stdout = self._old


# ── entry (import preview) serialization ──────────────────────────────────────

def _dest_exists(root, td) -> bool:
    """Would this entry's tag-derived destination album already exist in the library?"""
    if root is None or not (td.get("ALBUMARTIST") and td.get("YEAR") and td.get("TALB")):
        return False
    from import_tracks import sanitize_name
    dest = Path(root) / sanitize_name(td["ALBUMARTIST"]) / \
        sanitize_name(f"{td['YEAR']} - {td['TALB']}")
    return dest.is_dir() and any(dest.glob("*.mp3"))


def _serialize_entries(entries, root=None) -> list[dict]:
    from convert_lossless import LOSSLESS_EXTENSIONS
    rows = []
    for i, (path, td) in enumerate(entries):
        rows.append({
            "i": i,
            "src": str(path),
            "name": Path(path).name,
            "artist": td.get("TPE1", ""),
            "albumartist": td.get("ALBUMARTIST", ""),
            "title": td.get("TIT2", ""),
            "album": td.get("TALB", ""),
            "genre": td.get("TCON", ""),
            "year": td.get("YEAR", ""),
            "track": td.get("TRCK", ""),
            "disc": td.get("TPOS", ""),
            "bitrate": td.get("_MP3_BITRATE", ""),
            "lossless": Path(path).suffix.lower() in LOSSLESS_EXTENSIONS,
            "conflict": _dest_exists(root, td),
        })
    return rows


def _apply_entry_edits(entries, edited) -> None:
    """Apply browser-edited fields back onto the in-place (Path, tagdict) list.
    Tags map to ID3 frames; bitrate/art/conflict ride in private (underscore) keys
    that import_tracks consumes (lossless bitrate, cover choice, conflict add/skip)."""
    if not edited:
        return
    rows = [row for row in edited if isinstance(row, dict)]
    by_i = {row.get("i"): row for row in rows}
    order = {row.get("i"): pos for pos, row in enumerate(rows)}   # submitted order
    field_map = {"artist": "TPE1", "albumartist": "ALBUMARTIST", "title": "TIT2",
                 "album": "TALB", "genre": "TCON", "year": "YEAR", "track": "TRCK"}
    for i, (_path, td) in enumerate(entries):
        row = by_i.get(i)
        if not row:
            continue
        if i in order:
            td["_ORDER"] = order[i]
        for key, frame in field_map.items():
            if key in row:
                td[frame] = row[key]
        if row.get("bitrate"):
            try:
                td["_LOSSLESS_BITRATE"] = int(row["bitrate"])
            except (TypeError, ValueError):
                pass
        if row.get("art_none"):
            td["_ART_NONE"] = True
        elif row.get("art_url"):
            td["_ART_URL"] = row["art_url"]
        if row.get("conflict") in ("add", "skip"):
            td["_CONFLICT"] = row["conflict"]


# ── Runners ───────────────────────────────────────────────────────────────────

_STD_STEP_NAMES = [
    "Merge disc subfolders", "Fix missing tags", "Enforce ID3v2.3",
    "Strip extraneous tags", "Normalize special characters", "Normalize years",
    "Renumber tracks", "Zero-pad track numbers", "Set total track counts",
    "Rename album folders", "Deduplicate albums", "Rename artist folders",
    "Rename MP3 files", "Clean files",
]


def _run_standardize(job: Job, params: dict) -> None:
    import standardize as std
    from convert_lossless import step_convert_lossless

    root = Path(params["path"])
    dry_run = bool(params.get("dry_run", False))
    cfg = settings_mod.load(root)
    cover_art = cfg.get("cover_art", "folder")
    cover_art_size = cfg.get("cover_art_embed_size", 500)
    fetch_art_online = cfg.get("fetch_art_online", False)
    enforce_artist = cfg.get("enforce_artist_equals_album_artist", False)
    replace_brackets = cfg.get("replace_brackets_with_parentheses", False)
    preserve_replay_gain = cfg.get("preserve_replay_gain", False)
    preserve_tcmp = cfg.get("preserve_tcmp", False)
    preserve_disc_numbers = cfg.get("preserve_disc_numbers", False)
    keep_apic = cover_art in ("embed", "both")

    at = job.ask_text
    ac = job.ask_choice

    print(f"Directory : {root}")
    print(f"Cover art : {cover_art}")
    print(f"Mode      : {'DRY RUN' if dry_run else 'LIVE'}")

    job.set_progress("Step 0: Convert lossless files")
    job.add_sep("Step 0: Convert lossless files")
    step_convert_lossless(root, dry_run, ask_choice=ac)

    for idx, fn in enumerate(std.STEPS, 1):
        if job.cancelled:
            print("\nCancelled.")
            break
        name = _STD_STEP_NAMES[idx - 1] if idx <= len(_STD_STEP_NAMES) else fn.__name__
        job.set_progress(f"Step {idx}: {name}")
        job.add_sep(f"Step {idx}: {name}")
        if fn is std.step_strip_tags:
            fn(root, dry_run, keep_apic=keep_apic, keep_replay_gain=preserve_replay_gain,
               keep_tcmp=preserve_tcmp, keep_tpos=preserve_disc_numbers)
        elif fn is std.step_merge_subfolders:
            fn(root, dry_run, preserve_tpos=preserve_disc_numbers)
        elif fn is std.step_renumber_tracks:
            fn(root, dry_run, respect_tpos=preserve_disc_numbers)
        elif fn is std.step_pad_tracks:
            fn(root, dry_run, respect_tpos=preserve_disc_numbers)
        elif fn is std.step_set_total_tracks:
            fn(root, dry_run, respect_tpos=preserve_disc_numbers)
        elif fn is std.step_normalize_year and replace_brackets:
            std.step_replace_title_brackets(root, dry_run)
            fn(root, dry_run)
        elif fn is std.step_clean_files:
            fn(root, dry_run, cover_art=cover_art, ask_choice=ac)
        elif fn is std.step_rename_files and enforce_artist:
            std.step_enforce_track_artist(root, dry_run)
            fn(root, dry_run)
        elif fn is std.step_fix_missing_tags:
            fn(root, dry_run, ask_text=at)
        elif fn is std.step_rename_artist_folders:
            fn(root, dry_run, ask_choice=ac)
        else:
            fn(root, dry_run)

    if cover_art in ("embed", "both") and not job.cancelled:
        job.set_progress("Step 15: Embed cover art")
        job.add_sep("Step 15: Embed cover art")
        std.step_embed_cover_art(root, dry_run, max_size=cover_art_size,
                                 delete_covers=(cover_art == "embed"))

    if fetch_art_online and not job.cancelled:
        job.set_progress("Step 16: Fetch missing album art online")
        job.add_sep("Step 16: Fetch missing album art online")
        std.step_fetch_missing_art(root, dry_run, settings=cfg,
                                   cover_art=cover_art, max_size=cover_art_size)

    print("\nDone.")


def _run_import(job: Job, params: dict) -> None:
    from import_tracks import import_tracks

    root = Path(params["path"])
    source = Path(params["source"])
    dry_run = bool(params.get("dry_run", False))
    cfg = settings_mod.load(root)
    # Used by preview_fn to compute conflicts and the default lossless bitrate.
    job.library_root = root
    job.import_bitrate = cfg.get("import_bitrate", 320)

    try:
        import_tracks(
            source, root, dry_run,
            cover_art=cfg.get("cover_art", "folder"),
            cover_art_size=cfg.get("cover_art_embed_size", 500),
            settings=cfg,
            preview_fn=job.preview_fn,   # all editing is graphical; no text/choice prompts
            overall=job.import_progress,  # total-progress bar + ETA (not per-file)
        )
        print("\nDone.")
    finally:
        # Drag-and-drop imports run from a temp upload dir we own — remove it.
        if params.get("cleanup_source"):
            import shutil
            shutil.rmtree(source, ignore_errors=True)


def _run_sync(job: Job, params: dict) -> None:
    import shutil
    import sync_library as sync

    library = Path(params["path"])
    device = Path(params["device"])
    dry_run = bool(params.get("dry_run", False))
    selection = params.get("selection", {})

    artists = sync.artists_from_selection(selection)
    plan = sync.combined_plan(library, device, artists)
    usage = shutil.disk_usage(device)
    net = max(0, plan.bytes_to_copy - plan.bytes_to_remove)

    print(f"Device : {device}")
    print(f"Mode   : {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Copy   : {len(plan.copy_files)} files ({sync.format_size(plan.bytes_to_copy)})")
    print(f"Delete : {len(plan.remove_files)} files ({sync.format_size(plan.bytes_to_remove)})")
    print(f"Free   : {sync.format_size(usage.free)}  (net needed {sync.format_size(net)})")

    if net > usage.free:
        raise RuntimeError(
            f"Not enough free space: need {sync.format_size(net)}, "
            f"free {sync.format_size(usage.free)}")
    if not (plan.copy_files or plan.remove_files or plan.remove_dirs):
        print("\nNothing to do — device already in sync.")
        return

    start = time.monotonic()

    def on_progress(action, name, df, tf, db, tb):
        percent = int(db / tb * 100) if tb else (int(df / tf * 100) if tf else 0)
        eta = "" if dry_run else _eta(db, tb, time.monotonic() - start)
        job.set_progress(
            f"{df}/{tf} files · {sync.format_size(db)} / {sync.format_size(tb)}{eta}",
            percent=min(100, max(0, percent)))

    copied, rf, rd = sync.run_plan(plan, dry_run, on_progress=on_progress)
    verb_c = "Would copy" if dry_run else "Copied"
    verb_d = "would delete" if dry_run else "deleted"
    print(f"\n{verb_c} {copied}, {verb_d} {rf}, removed {rd} folders.")
    print("Dry run complete." if dry_run else "Sync complete.")


_RUNNERS = {
    "standardize": _run_standardize,
    "import": _run_import,
    "sync": _run_sync,
}


# ── Manager (single active job) ───────────────────────────────────────────────

class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _active(self) -> Job | None:
        for job in self.jobs.values():
            if job.state in ("running", "waiting"):
                return job
        return None

    def start(self, kind: str, params: dict) -> Job:
        runner = _RUNNERS.get(kind)
        if runner is None:
            raise ValueError(f"unknown job kind: {kind!r}")
        with self._lock:
            if self._active() is not None:
                raise RuntimeError("another operation is already running")
            job = Job(kind)
            self.jobs[job.id] = job

        def worker() -> None:
            try:
                with _Capture(job):
                    runner(job, params)
            except BaseException as exc:  # noqa: BLE001 — surface to client
                job.error = str(exc)
                job.append_log(f"ERROR: {exc}")
                job.append_log(traceback.format_exc().rstrip())
                job.state = "error"
            finally:
                job.set_progress("")
                if job.state != "error":
                    job.state = "done"

        job._thread = threading.Thread(target=worker, daemon=True)
        job._thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def active(self) -> Job | None:
        """The job currently running or waiting on input, if any."""
        return self._active()

    def respond(self, job_id: str, value) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.respond(value)
        return True

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel()
        return True


MANAGER = JobManager()
