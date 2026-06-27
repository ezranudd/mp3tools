#!/usr/bin/env python3
"""
mp3tools web UI — a thin FastAPI shell over the existing library modules.

No business logic lives here: every endpoint delegates to functions in
``browse``, ``fetch_art`` and ``settings`` (see WEBUI_TASK.md for the seam).
The frontend is the build-step-free ``index.html`` served at ``/``.

    python server.py ~/Music        # then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audit
import browse
import fetch_art
import settings as settings_mod
import sync_library as sync
from webjobs import MANAGER

# ── Library root ──────────────────────────────────────────────────────────────
# Set by main() (or by tests via set_root) before requests are served.
ROOT: Path = Path.cwd()

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "index.html"
_STATIC = _HERE / "static"
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def set_root(path) -> None:
    """Point the server at a library root (used by main() and tests)."""
    global ROOT
    ROOT = Path(path).expanduser().resolve()


def _safe(raw: str) -> Path:
    """Resolve *raw* and reject anything outside the library root."""
    p = Path(raw).expanduser().resolve()
    if p != ROOT and ROOT not in p.parents:
        raise HTTPException(status_code=403, detail="path outside library root")
    return p


def _device(raw: str) -> Path:
    """Resolve a sync device path: a real directory outside the library."""
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="device is not a directory")
    if p == ROOT or ROOT in p.parents:
        raise HTTPException(status_code=400,
                            detail="device cannot be the library or inside it")
    return p


def _validate_selection(selection: dict) -> None:
    """Ensure every artist/album path in a sync selection is inside the library."""
    for apath, sel in selection.items():
        _safe(apath)
        if isinstance(sel, (list, tuple)):
            for album in sel:
                _safe(album)


def _require_idle() -> None:
    """Reject library-mutating requests while a background operation runs."""
    job = MANAGER.active()
    if job is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a {job.kind} operation is running — try again when it finishes")


app = FastAPI(title="mp3tools web")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    # The frontend is plain ES modules served from /static; force the browser to
    # revalidate so edits show up on a normal reload (no stale cached modules).
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── Tree ──────────────────────────────────────────────────────────────────────

def _node_json(node: browse.Node) -> dict:
    return {
        "kind": node.kind,
        "label": node.label,
        "path": str(node.path),
        "children": [_node_json(c) for c in node.children],
    }


@app.get("/api/tree")
def api_tree() -> JSONResponse:
    artists = browse.build_tree(ROOT)
    return JSONResponse({
        "root": str(ROOT),
        "artists": [_node_json(a) for a in artists],
    })


# ── Album / tracks ────────────────────────────────────────────────────────────

@app.get("/api/album")
def api_album(path: str = Query(...)) -> JSONResponse:
    album = _safe(path)
    if not album.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    tracks = []
    for mp3 in sorted(album.glob("*.mp3")):
        tags = browse.read_tags(mp3)
        tracks.append({
            "path": str(mp3),
            "label": browse.track_label(tags, mp3.name),
            **tags,
        })
    return JSONResponse({"path": str(album), "tracks": tracks})


# ── Audio streaming ───────────────────────────────────────────────────────────

@app.get("/api/track")
def api_track(path: str = Query(...)) -> FileResponse:
    mp3 = _safe(path)
    if not (mp3.is_file() and mp3.suffix.lower() == ".mp3"):
        raise HTTPException(status_code=404, detail="track not found")
    # FileResponse handles HTTP Range requests, so seeking/streaming work.
    return FileResponse(mp3, media_type="audio/mpeg")


# ── Cover art ─────────────────────────────────────────────────────────────────

@app.get("/api/cover")
def api_cover(path: str = Query(...)) -> Response:
    album = _safe(path)
    if album.is_dir():
        for child in sorted(album.iterdir()):
            if child.is_file() and child.suffix.lower() in _IMAGE_EXTS \
                    and child.stem.lower() == "cover":
                return FileResponse(child)
        mp3s = sorted(album.glob("*.mp3"))
        target = mp3s[0] if mp3s else None
    else:
        target = album if album.suffix.lower() == ".mp3" else None

    if target is not None and target.is_file():
        try:
            from mutagen.id3 import ID3
            apic = ID3(target, translate=False).getall("APIC")
            if apic:
                return Response(apic[0].data,
                                media_type=apic[0].mime or "image/jpeg")
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="no cover art")


# ── Tag writes ────────────────────────────────────────────────────────────────

class TagUpdate(BaseModel):
    path: str
    updates: dict[str, str]


@app.post("/api/tags")
def api_tags(body: TagUpdate) -> JSONResponse:
    _require_idle()
    mp3 = _safe(body.path)
    if not mp3.is_file():
        raise HTTPException(status_code=404, detail="track not found")
    browse.write_tags(mp3, body.updates)
    tags = browse.read_tags(mp3)
    return JSONResponse({
        "path": str(mp3),
        "label": browse.track_label(tags, mp3.name),
        **tags,
    })


# ── Artwork search / apply ────────────────────────────────────────────────────

@app.get("/api/art/search")
def api_art_search(artist: str = Query(""), album: str = Query("")) -> JSONResponse:
    cfg = settings_mod.load(ROOT)
    try:
        results = fetch_art.search_art_sources(artist, album, cfg, interactive=True)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return JSONResponse({"results": results})


class ArtApply(BaseModel):
    path: str          # album directory
    url: str           # full-size artwork url to download


@app.post("/api/art/apply")
def api_art_apply(body: ArtApply) -> JSONResponse:
    _require_idle()
    album_dir = _safe(body.path)
    if not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    cfg = settings_mod.load(ROOT)
    try:
        data, mime = fetch_art.fetch_artwork(body.url)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Build an ALBUM node so we reuse browse's apply logic verbatim.
    album = browse.Node(browse.ALBUM, album_dir.name, album_dir)
    album.children = browse._make_tracks(sorted(album_dir.glob("*.mp3")), album)
    updated, errors = browse.apply_art_to_album(
        album, data, mime, cfg["cover_art"], cfg["cover_art_embed_size"])
    return JSONResponse({"updated": updated, "errors": errors})


# ── Audit ─────────────────────────────────────────────────────────────────────

@app.get("/api/audit")
def api_audit(path: str = Query("")) -> JSONResponse:
    target = _safe(path) if path else ROOT
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="path not found")
    return JSONResponse(audit.scan_json(target))


# ── Tree edits (rename / merge / move / retag) ────────────────────────────────

class EditRequest(BaseModel):
    path: str          # node path: artist dir, album dir, or track file
    op: str            # one of browse._EDIT_BUILDERS keys
    value: str


@app.post("/api/edit/preview")
def api_edit_preview(body: EditRequest) -> JSONResponse:
    node = _safe(body.path)
    try:
        edit = browse.build_edit(ROOT, node, body.op, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if edit is None:
        return JSONResponse({"ok": False, "desc": "", "changes": 0})
    changes = (len(edit.tag_writes) + len(edit.file_renames)
               + len(edit.dir_renames) + len(edit.dir_removals))
    return JSONResponse({"ok": True, "desc": edit.desc, "changes": changes})


@app.post("/api/edit/apply")
def api_edit_apply(body: EditRequest) -> JSONResponse:
    _require_idle()
    node = _safe(body.path)
    try:
        edit = browse.build_edit(ROOT, node, body.op, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if edit is None:
        raise HTTPException(status_code=400, detail="invalid or empty edit")
    new_path = _edit_new_path(edit, node)
    ok, error = browse.apply_edits([edit])
    return JSONResponse({"ok": ok, "desc": edit.desc, "error": error,
                         "new_path": str(new_path)})


def _edit_new_path(edit, node: Path) -> Path:
    """Best-effort path of *node* after the edit (album folder moves)."""
    for old, new in edit.dir_renames:
        if old == node:
            return new
    if node in edit.dir_removals and edit.file_renames:   # merge: tracks move out
        return edit.file_renames[0][1].parent
    return node


# ── Art removal ───────────────────────────────────────────────────────────────

class ArtRemove(BaseModel):
    path: str          # album directory
    mode: str          # "folder" | "embed" | "both"


@app.post("/api/art/remove")
def api_art_remove(body: ArtRemove) -> JSONResponse:
    _require_idle()
    album_dir = _safe(body.path)
    if not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    if body.mode not in ("folder", "embed", "both"):
        raise HTTPException(status_code=400, detail="bad mode")
    album = browse.Node(browse.ALBUM, album_dir.name, album_dir)
    removed, errors = browse._remove_art_from_album(album, body.mode)
    return JSONResponse({"removed": removed, "errors": errors})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
def api_get_settings() -> JSONResponse:
    return JSONResponse(settings_mod.load(ROOT))


@app.post("/api/settings")
def api_set_settings(body: dict) -> JSONResponse:
    cfg = settings_mod.load(ROOT)
    cfg.update(body)
    settings_mod.save(ROOT, cfg)
    return JSONResponse(settings_mod.load(ROOT))


# ── Sync (mirror selected artists/albums to a device) ─────────────────────────

@app.get("/api/sync/devices")
def api_sync_devices() -> JSONResponse:
    return JSONResponse({"devices": sync.device_rows(exclude=ROOT)})


@app.get("/api/sync/artists")
def api_sync_artists(device: str = Query(...)) -> JSONResponse:
    dev = _device(device)
    return JSONResponse({"device": str(dev), "artists": sync.artist_rows(ROOT, dev)})


@app.get("/api/sync/albums")
def api_sync_albums(artist: str = Query(...), device: str = Query(...)) -> JSONResponse:
    apath = _safe(artist)
    dev = _device(device)
    return JSONResponse(sync.album_rows(apath, dev))


class SyncPlanReq(BaseModel):
    device: str
    selection: dict


@app.post("/api/sync/plan")
def api_sync_plan(body: SyncPlanReq) -> JSONResponse:
    dev = _device(body.device)
    _validate_selection(body.selection)
    artists = sync.artists_from_selection(body.selection)
    plan = sync.combined_plan(ROOT, dev, artists)
    return JSONResponse(sync.plan_summary(plan, dev))


# ── Jobs (interactive operations: standardize, import, sync) ──────────────────

class JobStart(BaseModel):
    kind: str                  # "standardize" | "import" | "sync"
    dry_run: bool = False
    source: str = ""           # import only: source directory (may be outside root)
    device: str = ""           # sync only: device directory (outside root)
    selection: dict = {}       # sync only: {artist_path: "all" | [album_paths]}


@app.post("/api/jobs")
def api_start_job(body: JobStart) -> JSONResponse:
    params: dict = {"path": str(ROOT), "dry_run": body.dry_run}
    if body.kind == "import":
        src = Path(body.source).expanduser().resolve()
        if not src.is_dir():
            raise HTTPException(status_code=400, detail="source is not a directory")
        params["source"] = str(src)
    elif body.kind == "sync":
        params["device"] = str(_device(body.device))
        _validate_selection(body.selection)
        params["selection"] = body.selection
    try:
        job = MANAGER.start(body.kind, params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse({"job_id": job.id})


@app.get("/api/jobs/active")
def api_active_job() -> JSONResponse:
    """The currently running/waiting job (for the UI to resume after a reload)."""
    job = MANAGER.active()
    return JSONResponse({"active": job.to_json() if job else None})


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str) -> JSONResponse:
    job = MANAGER.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse(job.to_json())


@app.post("/api/jobs/{job_id}/respond")
def api_job_respond(job_id: str, body: dict) -> JSONResponse:
    if not MANAGER.respond(job_id, body.get("value")):
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str) -> JSONResponse:
    if not MANAGER.cancel(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse({"ok": True})


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if _INDEX.is_file():
        return HTMLResponse(_INDEX.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="index.html missing")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="mp3tools web UI")
    parser.add_argument("root", help="library root directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--desktop", action="store_true",
                        help="open in a native window (requires pywebview)")
    args = parser.parse_args()

    set_root(args.root)
    if not ROOT.is_dir():
        parser.error(f"not a directory: {ROOT}")

    url = f"http://{args.host}:{args.port}"
    if args.desktop:
        import threading
        import uvicorn
        import webview  # pywebview
        threading.Thread(
            target=lambda: uvicorn.run(app, host=args.host, port=args.port,
                                       log_level="warning"),
            daemon=True,
        ).start()
        webview.create_window("mp3tools", url)
        webview.start()
    else:
        import uvicorn
        print(f"mp3tools web — serving {ROOT}\n  {url}")
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
