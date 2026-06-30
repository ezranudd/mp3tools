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
import getpass
import ipaddress
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audit
import browse
import fetch_art
import rip_cd
import settings as settings_mod
import sync_library as sync
import webauth
from webjobs import MANAGER

# ── Library root ──────────────────────────────────────────────────────────────
# Set by main() (or by tests via set_root) before requests are served.
ROOT: Path = Path.cwd()

# True when started with --lan (bound to 0.0.0.0 for the local network). Only
# affects what we print and the /api/whoami hint — guest gating keys off the
# request's client IP, not this flag (see _is_owner / the access middleware).
LAN_MODE: bool = False

# Remote (internet) access. Off by default → behaviour is identical to before:
# loopback = owner, everyone else = read-only LAN guest, no auth involved. When
# enabled (--remote), the app sits behind a TLS reverse proxy (Caddy) that adds
# a shared secret header; only requests carrying it are treated as proxied, and
# proxied traffic can NEVER be owner (see _is_owner). Remote devices must log in
# (shared password) and be approved by the owner — see webauth + _access_gate.
REMOTE_MODE: bool = False
PROXY_SECRET: str = ""                   # from env MP3TOOLS_PROXY_SECRET
TRUSTED_PROXY: str = "127.0.0.1"         # only this peer may set X-Forwarded-For
_PROXY_HEADER = "x-mp3tools-proxy"

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "index.html"
_STATIC = _HERE / "static"
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _bg_file() -> Path:
    """Path to the web UI background image, inside the library's .mp3tools/ folder
    (ROOT is mutable, so compute per-call)."""
    return settings_mod.background_path(ROOT)


# Drag-and-drop import: opaque token → temp dir holding the uploaded source tree.
# Popped when the import job starts (which then owns cleanup of the dir).
_UPLOADS: dict[str, Path] = {}

# Source dir of the most recent import, used to bound /api/import/cover (the source
# lives outside ROOT). Set when an import job starts.
_IMPORT_SOURCE: Path | None = None

# ── Connected-device presence (passive) ───────────────────────────────────────
# The owner's "Devices" view (TRUSTED-only /api/admin/clients) reads this. It is
# populated as a *side effect* of requests clients already make — /api/track (the
# current song) and /api/whoami (bootstrap + foreground pings) — so we add no
# mutating endpoint and touch no guest rules. Keyed by a stable browser id (cid,
# from localStorage) so devices behind one NAT IP don't merge; falls back to IP.
_PRESENCE: dict[str, dict] = {}        # cid (or ip) → presence record
_PRESENCE_LOCK = threading.Lock()
_PRESENCE_TTL = 120                     # drop devices silent this many seconds


def _classify_ua(ua: str) -> dict:
    """Cheap, dependency-free User-Agent classification for the Devices view."""
    u = ua or ""
    lo = u.lower()
    device = "mobile" if any(s in lo for s in
                             ("mobi", "android", "iphone", "ipad", "ipod")) else "desktop"
    if "android" in lo:
        os_name = "Android"
    elif any(s in lo for s in ("iphone", "ipad", "ipod")):
        os_name = "iOS"
    elif "windows" in lo:
        os_name = "Windows"
    elif "mac os" in lo or "macintosh" in lo:
        os_name = "macOS"
    elif "linux" in lo:
        os_name = "Linux"
    else:
        os_name = "Unknown"
    # Order matters: Edge/Chrome UAs also contain "Safari"; Chrome contains neither
    # "Edg" first.
    if "edg" in lo:
        browser = "Edge"
    elif "firefox" in lo:
        browser = "Firefox"
    elif "chrome" in lo or "crios" in lo:
        browser = "Chrome"
    elif "safari" in lo:
        browser = "Safari"
    else:
        browser = "Unknown"
    return {"device": device, "os": os_name, "browser": browser}


def _touch_presence(request: Request, cid: str | None, *, track_path: str | None = None) -> None:
    """Upsert this client's presence record. Only overwrites the current track
    when *track_path* is given, so a /api/whoami ping refreshes last_seen without
    clobbering the song from the last /api/track request."""
    ip = _real_client_ip(request)
    key = cid or ip
    ua = request.headers.get("user-agent", "")
    now = time.time()
    with _PRESENCE_LOCK:
        rec = _PRESENCE.get(key)
        if rec is None:
            # A new record == a new connection (session). session_id/connected_at
            # let _active_presence finalize it to the connection log on prune.
            rec = {"first_seen": now, "connected_at": now,
                   "session_id": uuid.uuid4().hex[:12], "cid": cid or "",
                   "track_path": None}
            _PRESENCE[key] = rec
        rec.update(ip=ip, user_agent=ua, last_seen=now, **_classify_ua(ua))
        if track_path is not None:
            rec["track_path"] = track_path


def _active_presence() -> list[dict]:
    """Prune stale (idle) entries — finalizing each to the connection log — and
    return the survivors (copies)."""
    cutoff = time.time() - _PRESENCE_TTL
    with _PRESENCE_LOCK:
        stale = [k for k, r in _PRESENCE.items() if r["last_seen"] < cutoff]
        for key in stale:
            _append_session(_PRESENCE.pop(key))
        return [dict(r) for r in _PRESENCE.values()]


# ── GeoIP location (best-effort, off the request path) ────────────────────────
# Geographic location per device. LAN/private IPs are classified locally and
# never sent anywhere; only genuinely public IPs are looked up via a free no-key
# service (cached to disk). Resolution runs on a background daemon thread so it
# never blocks a guest's audio request — the owner's admin poll picks up the
# result on a later tick.
_GEO_API = "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,isp,query"
_GEO_CACHE: dict[str, dict] = {}        # ip ("__self__" = household) → geo dict ({} = looked-up-empty)
_GEO_INFLIGHT: set[str] = set()         # IPs a worker thread is currently resolving
_GEO_LOCK = threading.Lock()
_GEO_LOADED = False
_GEO_CACHE_CAP = 2000                    # bound memory + the on-disk cache file


def _ip_class(ip: str) -> str:
    """'loopback' | 'private' | 'public'. Unparseable (e.g. the 'local'
    sentinel) counts as loopback."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "loopback"
    if addr.is_loopback:
        return "loopback"
    if addr.is_private or addr.is_link_local:
        return "private"
    return "public"


def _geo_cache_file() -> Path:
    return settings_mod.settings_dir(ROOT) / "geoip-cache.json"


def _geo_load() -> None:
    """Lazily load the disk cache once (under the lock)."""
    global _GEO_LOADED
    if _GEO_LOADED:
        return
    try:
        with open(_geo_cache_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _GEO_CACHE.update(data)
    except Exception:
        pass
    _GEO_LOADED = True


def _geo_persist() -> None:
    """Write the (small) cache to disk. Caller holds _GEO_LOCK. Fail-soft."""
    try:
        d = settings_mod.settings_dir(ROOT)
        d.mkdir(parents=True, exist_ok=True)
        with open(_geo_cache_file(), "w", encoding="utf-8") as fh:
            json.dump(_GEO_CACHE, fh)
    except Exception:
        pass


def _geo_lookup(ip: str | None) -> dict:
    """Blocking GeoIP request (mirrors fetch_art's urllib style). Returns
    {city,region,country,isp} on success, else {} (so we don't retry forever).
    *ip* None resolves the server's own public IP (household)."""
    url = _GEO_API.format(ip=ip or "")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mp3tools/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {}
    if data.get("status") != "success":
        return {}
    return {
        "city": data.get("city", ""),
        "region": data.get("regionName", ""),
        "country": data.get("country", ""),
        "country_code": data.get("countryCode", ""),
        "isp": data.get("isp", ""),
    }


def _geo_resolve_async(key: str, ip: str | None) -> None:
    """Spawn a daemon worker to resolve *ip* into _GEO_CACHE[key], unless one is
    already in flight or it's already cached."""
    with _GEO_LOCK:
        _geo_load()
        if key in _GEO_CACHE or key in _GEO_INFLIGHT:
            return
        _GEO_INFLIGHT.add(key)

    def work() -> None:
        result = _geo_lookup(ip)
        with _GEO_LOCK:
            # Bound the cache (and its on-disk file): FIFO-evict the oldest
            # non-household entry once at capacity. dict keeps insertion order.
            if key not in _GEO_CACHE and len(_GEO_CACHE) >= _GEO_CACHE_CAP:
                for k in _GEO_CACHE:
                    if k != "__self__":
                        del _GEO_CACHE[k]
                        break
            _GEO_CACHE[key] = result
            _GEO_INFLIGHT.discard(key)
            _geo_persist()

    threading.Thread(target=work, daemon=True).start()


def _geo_get(key: str) -> dict | None:
    """Cached geo for *key*, or None if not resolved yet (or resolved empty)."""
    with _GEO_LOCK:
        _geo_load()
        return _GEO_CACHE.get(key) or None


def _household_geo() -> dict | None:
    """Approx city of the server's own public IP, shown for LAN clients."""
    _geo_resolve_async("__self__", None)
    return _geo_get("__self__")


def _location_for(ip: str) -> dict:
    """Display-ready location object for a client IP. Private/loopback IPs are
    never sent off-box; public IPs are resolved in the background."""
    cls = _ip_class(ip)
    if cls == "loopback":
        return {"label": "This device", "scope": "loopback"}
    if cls == "private":
        hh = _household_geo()
        label = "Local network"
        if hh and hh.get("city"):
            label += f" · {hh['city']}, {hh.get('country_code') or hh.get('country', '')}"
        return {"label": label, "scope": "private"}
    # public
    geo = _geo_get(ip)
    if geo is None:
        _geo_resolve_async(ip, ip)
        return {"label": "Locating…", "scope": "public"}
    if not geo.get("city") and not geo.get("country"):
        return {"label": "Unknown", "scope": "public"}
    place = ", ".join(p for p in (geo.get("city"), geo.get("country")) if p)
    return {"label": place, "scope": "public", "isp": geo.get("isp", ""),
            "region": geo.get("region", "")}


# ── Connection log (durable, one line per finished session) ───────────────────
_LOG_LOCK = threading.Lock()
_LOG_CAP = 10000                        # keep at most this many lines


def _log_file() -> Path:
    return settings_mod.settings_dir(ROOT) / "connections.log"


def _session_record(rec: dict, *, disconnected_at: float | None) -> dict:
    """Shape a presence record into a connection-log / history row."""
    start = rec.get("connected_at", rec.get("first_seen", 0))
    end = disconnected_at if disconnected_at is not None else rec.get("last_seen", start)
    return {
        "session_id": rec.get("session_id", ""),
        "cid": rec.get("cid", ""),
        "ip": rec.get("ip", ""),
        "device": rec.get("device", ""),
        "os": rec.get("os", ""),
        "browser": rec.get("browser", ""),
        "location": _location_for(rec.get("ip", "")).get("label", ""),
        "connected_at": start,
        "disconnected_at": disconnected_at,
        "duration_s": int(max(0, end - start)),
    }


def _append_session(rec: dict) -> None:
    """Finalize a pruned presence record to the connection log. Caller holds
    _PRESENCE_LOCK; this only touches the log file. Fail-soft."""
    row = _session_record(rec, disconnected_at=rec.get("last_seen"))
    with _LOG_LOCK:
        try:
            d = settings_mod.settings_dir(ROOT)
            d.mkdir(parents=True, exist_ok=True)
            path = _log_file()
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            _trim_log(path)
        except Exception:
            pass


def _trim_log(path: Path) -> None:
    """Cap the log at _LOG_CAP lines (caller holds _LOG_LOCK). Cheap & rare."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > _LOG_CAP:
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-_LOG_CAP:])
    except Exception:
        pass


def _read_log(limit: int) -> list[dict]:
    """Last *limit* finished sessions from the connection log, oldest→newest."""
    with _LOG_LOCK:
        try:
            with open(_log_file(), encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def set_root(path) -> None:
    """Point the server at a library root (used by main() and tests)."""
    global ROOT
    ROOT = Path(path).expanduser().resolve()
    webauth.configure(ROOT)


def _peer_is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return True                      # in-process / test calls
    host = client.host
    return host in ("127.0.0.1", "::1") or host.startswith("127.")


def _via_proxy(request: Request) -> bool:
    """True iff this request arrived through our trusted reverse proxy: it must
    carry the shared secret header AND come from the proxy's own address. Only
    then do we trust X-Forwarded-For — and such requests can never be owner."""
    if not REMOTE_MODE or not PROXY_SECRET:
        return False
    if request.headers.get(_PROXY_HEADER) != PROXY_SECRET:
        return False
    return _peer_is_loopback(request) or (
        request.client is not None and request.client.host == TRUSTED_PROXY)


def _is_owner(request: Request) -> bool:
    """Full access. Requires a DIRECT local socket — a proxied request (i.e. any
    internet visitor) is structurally excluded, so remote traffic can never gain
    owner/admin no matter what headers it forges."""
    if _via_proxy(request):
        return False
    return _peer_is_loopback(request)


# Backwards-compatible alias: "trusted" now means "owner" (the only full-access
# role). Used by the settings GET filter and admin endpoints.
_is_trusted = _is_owner


def _real_client_ip(request: Request) -> str:
    """The genuine client IP, used for rate-limiting/presence/geo (never for
    authorization). When the request came via our single trusted proxy, the real
    client is the LAST X-Forwarded-For hop — the one Caddy appended. The earlier
    hops are attacker-supplied (Caddy appends rather than replaces the header), so
    reading the first hop would let a remote client spoof its IP and defeat the
    per-IP login rate limiter."""
    if _via_proxy(request):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    client = request.client
    return client.host if client is not None else "local"


def _role(request: Request) -> str:
    """'owner' | 'member' | 'pending' | 'blocked' | 'lan' | 'anonymous'.
    Library/UI gating keys off this. When REMOTE_MODE is off there are no proxied
    requests, so only 'owner' and 'lan' occur — identical to the old model."""
    if _is_owner(request):
        return "owner"
    if _via_proxy(request):
        return webauth.resolve(request.cookies.get(webauth.COOKIE_NAME))
    return "lan"                         # direct non-loopback client (LAN guest)


def _require_owner(request: Request) -> None:
    """Explicit backstop for owner-only routes (the gate already blocks them for
    everyone else; this is defense-in-depth so a routing slip can't leak them)."""
    if not _is_owner(request):
        raise HTTPException(status_code=403, detail="owner only")


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

# Read-only routes a non-loopback "guest" may reach. Default-deny: anything not
# here (audit, edit, import, sync, jobs, settings POST, every mutation) → 403,
# so new endpoints are private unless deliberately added. Static + "/" handle
# the SPA; the SPA itself hides admin UI for guests (see /api/whoami).
_GUEST_GET_PATHS = frozenset({
    "/", "/api/tree", "/api/album", "/api/search", "/api/genre",
    "/api/track", "/api/cover", "/api/background", "/api/settings",
    "/api/whoami",
})


def _member_allowed(method: str, path: str) -> bool:
    """Read-only browse/play surface for LAN guests and approved remote members."""
    if method not in ("GET", "HEAD"):
        return False
    return path in _GUEST_GET_PATHS or path.startswith("/static/")


# Minimal surface an unauthenticated/pending remote client may reach: just enough
# to load the SPA shell, learn its role, and log in/out. Nothing else.
_AUTH_SURFACE_GET = frozenset({"/", "/api/whoami"})
_AUTH_SURFACE_POST = frozenset({"/api/auth/login", "/api/auth/logout"})


def _auth_surface_allowed(method: str, path: str) -> bool:
    if path.startswith("/static/") and method in ("GET", "HEAD"):
        return True
    if method in ("GET", "HEAD"):
        return path in _AUTH_SURFACE_GET
    if method == "POST":
        return path in _AUTH_SURFACE_POST
    return False


@app.middleware("http")
async def _access_gate(request: Request, call_next):
    # Role-based gate. owner → everything; member/lan → read-only allowlist;
    # pending/blocked → only the login surface (403); anonymous → login surface
    # (401 so the SPA shows the login screen). Mutations stay owner-only, so the
    # internet can never reach them.
    role = _role(request)
    method, path = request.method, request.url.path
    if role == "owner":
        return await call_next(request)
    # Every non-owner may always reach the shell + login/logout (so even a member
    # can sign out, and the SPA can always load to show its login/role state).
    if _auth_surface_allowed(method, path):
        return await call_next(request)
    if role in ("member", "lan"):
        if _member_allowed(method, path):
            return await call_next(request)
        return JSONResponse(status_code=403, content={"detail": "read-only"})
    if role == "anonymous":
        return JSONResponse(status_code=401, content={"detail": "login required"})
    # pending or blocked
    detail = "device blocked" if role == "blocked" else "awaiting approval"
    return JSONResponse(status_code=403, content={"detail": detail})


@app.middleware("http")
async def _no_cache_static(request, call_next):
    # The frontend is plain ES modules served from /static; force the browser to
    # revalidate so edits show up on a normal reload (no stale cached modules).
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# Conservative CSP: 'unsafe-inline' is required because the SPA uses inline
# style= attributes and a few inline onerror= image fallbacks (search/tree/
# player/import). The high-value directives are still locked down (no plugins,
# no framing, no <base> hijack, forms/XHR same-origin). Tightening to a nonce
# would mean refactoring those inline handlers — tracked as future work.
_CSP = ("default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'")


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    # HSTS only when actually reached over TLS via the trusted proxy (never on
    # plain-HTTP loopback, which would wrongly pin localhost to https).
    if _via_proxy(request):
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=63072000; includeSubDomains")
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


@app.get("/api/whoami")
def api_whoami(request: Request, cid: str = Query(None)) -> JSONResponse:
    """Bootstrap hint for the SPA. Returns the caller's role so the SPA can show
    the owner UI, a read-only browse/play UI, a login gate, or a waiting-for-
    approval screen. Doubles as a presence ping (cid, foreground warmup)."""
    role = _role(request)
    # Don't let unauthenticated/blocked remote callers plant presence rows (which
    # would later trigger owner-side GeoIP lookups for attacker-chosen IPs and
    # pollute the Devices view). Only track real users: the owner, LAN guests, and
    # password-authenticated remote devices.
    if role in ("owner", "lan", "member", "pending"):
        _touch_presence(request, cid)
    return JSONResponse({
        "role": role,
        "trusted": role == "owner",      # back-compat (SPA still reads this)
        "remote": _via_proxy(request),
        "lan": LAN_MODE,
        # Only meaningful in remote mode; lets the login screen warn if the owner
        # enabled --remote but never set a password.
        "password_set": webauth.password_set() if REMOTE_MODE else False,
    })


# ── Remote authentication (shared password + device approval) ─────────────────

@app.post("/api/auth/login")
def api_login(request: Request, body: dict) -> JSONResponse:
    """Authenticate a remote device with the shared access password. New devices
    land in the pending queue until the owner approves them. Rate-limited."""
    ip = _real_client_ip(request)
    # Atomically consume one attempt (counts as a failure until note_success).
    allowed, retry = webauth.login_attempt(ip)
    if not allowed:
        raise HTTPException(status_code=429,
                            detail=f"too many attempts — retry in {retry}s")
    body = body or {}
    pw = str(body.get("password", ""))
    cid = str(body.get("cid", "")).strip()
    name = str(body.get("device_name", "")).strip()
    # scrypt itself is the per-attempt cost and runs whether or not the password
    # matches, so failure timing is already constant. We deliberately avoid an
    # extra time.sleep() here: this sync endpoint runs on the bounded threadpool,
    # and a held sleep per request is a cheap DoS amplifier. Brute force is capped
    # by the per-IP rate limiter (login_attempt) above.
    if not webauth.verify_password(pw):
        raise HTTPException(status_code=401, detail="invalid password")
    webauth.note_success(ip)
    if not cid:
        raise HTTPException(status_code=400, detail="missing device id")
    if webauth.device_state(cid) == "blocked":
        raise HTTPException(status_code=403, detail="device blocked")
    state = webauth.register_pending(cid, ip, name)   # unknown → pending
    token = webauth.create_session(cid, ip)
    role = "member" if state == "approved" else "pending"
    resp = JSONResponse({"role": role})
    resp.set_cookie(webauth.COOKIE_NAME, token, max_age=webauth.SESSION_TTL,
                    httponly=True, secure=True, samesite="strict", path="/")
    return resp


@app.post("/api/auth/logout")
def api_logout(request: Request) -> JSONResponse:
    webauth.destroy_session(request.cookies.get(webauth.COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(webauth.COOKIE_NAME, path="/")
    return resp


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


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/api/search")
def api_search(q: str = Query(""), limit: int = Query(20)) -> JSONResponse:
    return JSONResponse(browse.search(ROOT, q, limit))


@app.get("/api/genre")
def api_genre(name: str = Query("")) -> JSONResponse:
    return JSONResponse({"genre": name, "albums": browse.albums_by_genre(ROOT, name)})


# ── Audio streaming ───────────────────────────────────────────────────────────

@app.get("/api/track")
def api_track(request: Request, path: str = Query(...), cid: str = Query(None)) -> FileResponse:
    mp3 = _safe(path)
    if not (mp3.is_file() and mp3.suffix.lower() == ".mp3"):
        raise HTTPException(status_code=404, detail="track not found")
    _touch_presence(request, cid, track_path=str(mp3))
    # FileResponse handles HTTP Range requests, so seeking/streaming work.
    return FileResponse(mp3, media_type="audio/mpeg")


# ── Admin: connected devices ──────────────────────────────────────────────────

@app.get("/api/admin/clients")
def api_admin_clients() -> JSONResponse:
    """Connected devices and what each is playing. TRUSTED-only: deliberately
    absent from _GUEST_GET_PATHS, so _guest_gate 403s non-loopback clients."""
    now = time.time()
    clients = []
    for rec in _active_presence():
        tp = rec.get("track_path")
        now_playing = None
        if tp:
            mp3 = Path(tp)
            try:
                tags = browse.read_tags(mp3)
                now_playing = {
                    "path": tp,
                    "title": tags.get("title") or browse.track_label(tags, mp3.name),
                    "artist": tags.get("artist", ""),
                    "album": tags.get("album", ""),
                }
            except Exception:
                now_playing = {"path": tp, "title": mp3.name, "artist": "", "album": ""}
        ip = rec["ip"]
        clients.append({
            "ip": ip,
            "device": rec["device"],
            "os": rec["os"],
            "browser": rec["browser"],
            "you": ip in ("127.0.0.1", "::1", "local") or ip.startswith("127."),
            "location": _location_for(ip),
            "now_playing": now_playing,
            "first_seen": rec["first_seen"],
            "last_seen": rec["last_seen"],
            "idle_seconds": int(now - rec["last_seen"]),
        })
    clients.sort(key=lambda c: c["last_seen"], reverse=True)
    return JSONResponse({"clients": clients})


@app.get("/api/admin/connections")
def api_admin_connections(limit: int = Query(200)) -> JSONResponse:
    """Browsable connection history: finished sessions from the durable log plus
    the currently-active ones (synthesized live). TRUSTED-only — deliberately
    absent from _GUEST_GET_PATHS. Newest first."""
    limit = max(1, min(limit, 2000))
    conns = _read_log(limit)
    active_ids = set()
    for rec in _active_presence():
        row = _session_record(rec, disconnected_at=None)
        row["active"] = True
        active_ids.add(row["session_id"])
        conns.append(row)
    # A session may exist both as a (just-finalized) log line and an active row
    # in a race; prefer the active one.
    seen = set()
    merged = []
    for row in conns:
        sid = row.get("session_id")
        if sid and sid in active_ids and not row.get("active"):
            continue
        if sid and sid in seen:
            continue
        if sid:
            seen.add(sid)
        merged.append(row)
    merged.sort(key=lambda c: c.get("connected_at", 0), reverse=True)
    return JSONResponse({"connections": merged[:limit]})


# ── Admin: remote access control (owner-only) ─────────────────────────────────

@app.get("/api/admin/access")
def api_admin_access(request: Request) -> JSONResponse:
    """The device whitelist + approval queue, enriched with live presence
    (online/last-seen/location). Owner-only."""
    _require_owner(request)
    # Map cid → live presence so the list can show who's currently connected.
    pres = {r.get("cid"): r for r in _active_presence() if r.get("cid")}
    now = time.time()
    devices = []
    for d in webauth.list_devices():
        cid = d["cid"]
        p = pres.get(cid)
        ip = (p or {}).get("ip") or d.get("ip", "")
        devices.append({
            "cid": cid,
            "name": d.get("name", ""),
            "state": d.get("state", "pending"),
            "ip": ip,
            "location": _location_for(ip) if ip else None,
            "first_seen": d.get("first_seen"),
            "approved_at": d.get("approved_at"),
            "online": p is not None,
            "last_seen": (p or {}).get("last_seen"),
            "idle_seconds": int(now - p["last_seen"]) if p else None,
        })
    # Pending first, then online, then most-recently-seen.
    devices.sort(key=lambda x: (x["state"] != "pending", not x["online"],
                                -(x["last_seen"] or x["first_seen"] or 0)))
    return JSONResponse({
        "password_set": webauth.password_set(),
        "remote_enabled": REMOTE_MODE,
        "devices": devices,
    })


@app.post("/api/admin/access/approve")
def api_admin_approve(request: Request, body: dict) -> JSONResponse:
    _require_owner(request)
    cid = str((body or {}).get("cid", "")).strip()
    if not cid:
        raise HTTPException(status_code=400, detail="missing cid")
    webauth.approve(cid, str((body or {}).get("name", "")).strip())
    return JSONResponse({"ok": True})


@app.post("/api/admin/access/block")
def api_admin_block(request: Request, body: dict) -> JSONResponse:
    _require_owner(request)
    cid = str((body or {}).get("cid", "")).strip()
    if not cid:
        raise HTTPException(status_code=400, detail="missing cid")
    webauth.block(cid)
    return JSONResponse({"ok": True})


@app.post("/api/admin/access/revoke")
def api_admin_revoke(request: Request, body: dict) -> JSONResponse:
    _require_owner(request)
    cid = str((body or {}).get("cid", "")).strip()
    if not cid:
        raise HTTPException(status_code=400, detail="missing cid")
    webauth.revoke(cid)
    return JSONResponse({"ok": True})


@app.post("/api/admin/access/rename")
def api_admin_rename(request: Request, body: dict) -> JSONResponse:
    _require_owner(request)
    cid = str((body or {}).get("cid", "")).strip()
    name = str((body or {}).get("name", "")).strip()
    if not cid:
        raise HTTPException(status_code=400, detail="missing cid")
    webauth.rename(cid, name)
    return JSONResponse({"ok": True})


@app.post("/api/admin/access/password")
def api_admin_set_password(request: Request, body: dict) -> JSONResponse:
    """Set/rotate the shared access password (owner-only; rotating logs everyone
    out). Reachable from localhost even before --remote is enabled, so the owner
    can provision a password from the Settings/Access UI."""
    _require_owner(request)
    pw = str((body or {}).get("password", ""))
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    webauth.set_password(pw)
    return JSONResponse({"ok": True, "password_set": True})


# ── Cover art ─────────────────────────────────────────────────────────────────

def _cover_response(album: Path) -> Response:
    """Serve an album's cover: a cover.* image in the folder, else the first mp3's
    embedded APIC. Raises 404 if neither exists. (Path validation is the caller's job.)"""
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


@app.get("/api/cover")
def api_cover(path: str = Query(...)) -> Response:
    return _cover_response(_safe(path))


@app.get("/api/import/cover")
def api_import_cover(path: str = Query(...)) -> Response:
    """Cover for a to-be-imported album folder. Bounded to the active import source
    dir (which lives outside ROOT, so _safe/api_cover can't be used)."""
    target = Path(path).expanduser().resolve()
    if _IMPORT_SOURCE is None:
        raise HTTPException(status_code=404, detail="no active import")
    if target != _IMPORT_SOURCE and _IMPORT_SOURCE not in target.parents:
        raise HTTPException(status_code=403, detail="path outside import source")
    return _cover_response(target)


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


def _apply_album_art(album_dir: Path, data: bytes, mime: str) -> dict:
    """Apply image *data* as *album_dir*'s cover, honoring the cover-art settings."""
    cfg = settings_mod.load(ROOT)
    # Build an ALBUM node so we reuse browse's apply logic verbatim.
    album = browse.Node(browse.ALBUM, album_dir.name, album_dir)
    album.children = browse._make_tracks(sorted(album_dir.glob("*.mp3")), album)
    updated, errors = browse.apply_art_to_album(
        album, data, mime, cfg["cover_art"], cfg["cover_art_embed_size"])
    return {"updated": updated, "errors": errors}


@app.post("/api/art/apply")
def api_art_apply(body: ArtApply) -> JSONResponse:
    _require_idle()
    album_dir = _safe(body.path)
    if not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    try:
        data, mime = fetch_art.fetch_artwork(body.url)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return JSONResponse(_apply_album_art(album_dir, data, mime))


@app.post("/api/art/upload")
async def api_art_upload(request: Request, path: str = Query(...)) -> JSONResponse:
    """Apply a user-supplied local image (raw body) as the album cover."""
    _require_idle()
    album_dir = _safe(path)
    if not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    mime = (request.headers.get("content-type") or "").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    try:
        return JSONResponse(_apply_album_art(album_dir, data, mime))
    except Exception as e:                       # bad/corrupt image, etc.
        raise HTTPException(status_code=400, detail=f"could not apply image: {e}")


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


class AlbumDelete(BaseModel):
    path: str          # album directory


@app.post("/api/album/delete")
def api_album_delete(body: AlbumDelete) -> JSONResponse:
    _require_idle()
    album_dir = _safe(body.path)                       # rejects paths outside ROOT
    if not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    if album_dir == ROOT or album_dir.parent == ROOT:  # never ROOT or an artist dir
        raise HTTPException(status_code=400, detail="not an album folder")
    shutil.rmtree(album_dir)
    artist_dir = album_dir.parent                      # prune the artist if now empty
    try:
        if artist_dir != ROOT and not any(artist_dir.iterdir()):
            artist_dir.rmdir()
    except OSError:
        pass
    return JSONResponse({"ok": True, "deleted": str(album_dir)})


class AlbumReorder(BaseModel):
    path: str            # album directory
    order: list[str]     # track file paths in their new order


@app.post("/api/album/reorder")
def api_album_reorder(body: AlbumReorder) -> JSONResponse:
    _require_idle()
    album_dir = _safe(body.path)
    if not album_dir.is_dir():
        raise HTTPException(status_code=404, detail="album not found")
    ordered = [_safe(p) for p in body.order]
    if any(p.parent != album_dir for p in ordered):
        raise HTTPException(status_code=400, detail="tracks must be in the album folder")
    ok, error = browse.reorder_album(album_dir, ordered)
    return JSONResponse({"ok": ok, "error": error})


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

def _settings_response() -> dict:
    """Settings plus derived background fields (presence/version from the file)."""
    cfg = settings_mod.load(ROOT)
    bg = _bg_file()
    cfg["background_present"] = bg.is_file()
    cfg["background_version"] = int(bg.stat().st_mtime) if bg.is_file() else 0
    return cfg


def _guest_settings(cfg: dict) -> dict:
    """Strip owner-only / credential-ish keys before handing settings to a guest.
    Guests only need display fields (background presence/version/fit/blur/opacity)."""
    drop = {"art_sources", "art_source_order"}
    return {k: v for k, v in cfg.items()
            if k not in drop
            and not any(s in k.lower() for s in ("token", "key", "secret"))}


@app.get("/api/settings")
def api_get_settings(request: Request) -> JSONResponse:
    cfg = _settings_response()
    if not _is_trusted(request):
        cfg = _guest_settings(cfg)
    return JSONResponse(cfg)


@app.post("/api/settings")
def api_set_settings(body: dict) -> JSONResponse:
    cfg = settings_mod.load(ROOT)
    cfg.update(body)
    settings_mod.save(ROOT, cfg)
    return JSONResponse(_settings_response())


# ── Background image (web UI personalization) ─────────────────────────────────

@app.post("/api/background")
async def api_background_upload(request: Request) -> JSONResponse:
    """Store a user-supplied image (raw body) as the full-window background."""
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    mime = (request.headers.get("content-type") or "").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    bg = _bg_file()
    bg.parent.mkdir(parents=True, exist_ok=True)
    bg.write_bytes(data)
    cfg = settings_mod.load(ROOT)
    cfg["background_mime"] = mime
    settings_mod.save(ROOT, cfg)
    return JSONResponse({"version": int(bg.stat().st_mtime)})


@app.get("/api/background")
def api_background_get() -> FileResponse:
    bg = _bg_file()
    if not bg.is_file():
        raise HTTPException(status_code=404, detail="no background image")
    mime = settings_mod.load(ROOT).get("background_mime") or "image/jpeg"
    return FileResponse(bg, media_type=mime)


@app.delete("/api/background")
def api_background_clear() -> JSONResponse:
    bg = _bg_file()
    if bg.is_file():
        bg.unlink()
    cfg = settings_mod.load(ROOT)
    cfg["background_mime"] = ""
    settings_mod.save(ROOT, cfg)
    return JSONResponse({})


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


# ── Drag-and-drop import upload ───────────────────────────────────────────────

@app.post("/api/import/upload/start")
def api_import_upload_start() -> JSONResponse:
    """Open an upload session: a temp dir that received files become the import source."""
    d = Path(tempfile.mkdtemp(prefix="mp3tools-upload-"))
    token = uuid.uuid4().hex
    _UPLOADS[token] = d
    return JSONResponse({"token": token})


@app.post("/api/import/upload/file")
async def api_import_upload_file(request: Request, token: str = Query(...),
                                path: str = Query(...)) -> JSONResponse:
    """Write one uploaded file (raw body) into the session dir, preserving *path*."""
    base = _UPLOADS.get(token)
    if base is None:
        raise HTTPException(status_code=404, detail="unknown upload token")
    dest = (base / path).resolve()
    if dest != base and base not in dest.parents:   # reject absolute / .. traversal
        raise HTTPException(status_code=400, detail="bad path")
    data = await request.body()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return JSONResponse({"ok": True})


# ── Rip (CD → temp dir → import) ──────────────────────────────────────────────

@app.get("/api/rip/devices")
def api_rip_devices() -> JSONResponse:
    """Optical drives present on the host running the server (owner-only)."""
    return JSONResponse({"devices": [str(d) for d in rip_cd.detect_cd_devices()]})


# ── Jobs (interactive operations: standardize, import, sync, rip) ─────────────

class JobStart(BaseModel):
    kind: str                  # "standardize" | "import" | "sync" | "rip"
    dry_run: bool = False
    source: str = ""           # import only: source directory (may be outside root)
    upload_token: str = ""     # import only: drag-and-drop upload session token
    device: str = ""           # sync: device dir; rip: optical device path
    selection: dict = {}       # sync only: {artist_path: "all" | [album_paths]}


@app.post("/api/jobs")
def api_start_job(body: JobStart) -> JSONResponse:
    global _IMPORT_SOURCE
    params: dict = {"path": str(ROOT), "dry_run": body.dry_run}
    if body.kind == "import":
        if body.upload_token:
            src = _UPLOADS.pop(body.upload_token, None)   # job now owns this temp dir
            if src is None or not src.is_dir():
                raise HTTPException(status_code=400, detail="unknown upload token")
            if not any(src.rglob("*")):
                shutil.rmtree(src, ignore_errors=True)
                raise HTTPException(status_code=400, detail="no files uploaded")
            params["source"] = str(src)
            params["cleanup_source"] = True
        else:
            src = Path(body.source).expanduser().resolve()
            if not src.is_dir():
                raise HTTPException(status_code=400, detail="source is not a directory")
            params["source"] = str(src)
        _IMPORT_SOURCE = Path(params["source"])   # bounds /api/import/cover
    elif body.kind == "sync":
        params["device"] = str(_device(body.device))
        _validate_selection(body.selection)
        params["selection"] = body.selection
    elif body.kind == "rip":
        valid = {str(d) for d in rip_cd.detect_cd_devices()}
        if body.device not in valid:
            raise HTTPException(status_code=400, detail="unknown CD device")
        params["device"] = body.device
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

def _lan_ip() -> str:
    """Best-effort local-network IP for the printed URL. No packets are sent —
    connecting a UDP socket just picks the outbound interface's address."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _set_password_cli() -> None:
    """Interactively set/rotate the remote-access password, then exit."""
    pw = getpass.getpass("New access password: ")
    if not pw:
        raise SystemExit("aborted: empty password")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("aborted: passwords did not match")
    webauth.set_password(pw)
    print("Access password set. Existing remote sessions were invalidated.")


def main() -> None:
    global LAN_MODE, REMOTE_MODE, PROXY_SECRET, TRUSTED_PROXY
    parser = argparse.ArgumentParser(description="mp3tools web UI")
    parser.add_argument("root", help="library root directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lan", action="store_true",
                        help="serve to the local network (binds 0.0.0.0); "
                             "remote devices get read-only browse + playback")
    parser.add_argument("--remote", action="store_true",
                        help="enable authenticated internet access behind a TLS "
                             "reverse proxy (Caddy). Requires env "
                             "MP3TOOLS_PROXY_SECRET; see Caddyfile.")
    parser.add_argument("--set-password", action="store_true",
                        help="set/rotate the remote-access password, then exit")
    parser.add_argument("--trusted-proxy", default="127.0.0.1",
                        help="address the reverse proxy connects from (default 127.0.0.1)")
    parser.add_argument("--desktop", action="store_true",
                        help="open in a native window (requires pywebview)")
    args = parser.parse_args()

    set_root(args.root)
    if not ROOT.is_dir():
        parser.error(f"not a directory: {ROOT}")

    if args.set_password:
        _set_password_cli()
        return

    host = args.host
    if args.lan:
        LAN_MODE = True
        if host == "127.0.0.1":          # honor an explicit --host if given
            host = "0.0.0.0"

    if args.remote:
        REMOTE_MODE = True
        TRUSTED_PROXY = args.trusted_proxy
        PROXY_SECRET = os.environ.get("MP3TOOLS_PROXY_SECRET", "")
        if not PROXY_SECRET:
            parser.error("--remote requires env MP3TOOLS_PROXY_SECRET to be set "
                         "(a long random string shared with the reverse proxy)")
        if not webauth.password_set():
            parser.error("--remote requires an access password — run "
                         f"'python server.py {args.root} --set-password' first")

    url = f"http://{host}:{args.port}"
    if args.desktop:
        import threading
        import uvicorn
        import webview  # pywebview
        threading.Thread(
            target=lambda: uvicorn.run(app, host=host, port=args.port,
                                       log_level="warning",
                                       timeout_keep_alive=65,
                                       proxy_headers=False),
            daemon=True,
        ).start()
        webview.create_window("mp3tools", url)
        webview.start()
    else:
        import uvicorn
        print(f"mp3tools web — serving {ROOT}\n  {url}")
        if LAN_MODE:
            print(f"  local network: http://{_lan_ip()}:{args.port}  "
                  "(read-only browse + playback)")
            print("  note: library is exposed read-only to the local network — "
                  "do not use on untrusted networks.")
        if REMOTE_MODE:
            print("  remote access: ENABLED behind a TLS reverse proxy "
                  "(password + device approval required).")
            print("  bound to loopback; point Caddy at this port (see Caddyfile).")
        # Long keep-alive so the server doesn't close idle connections out from
        # under an iOS PWA that still believes the socket is alive (default is 5s).
        # proxy_headers=False is critical: we must see the *real* socket peer (the
        # reverse proxy) and do X-Forwarded-For ourselves, gated by the secret
        # header (see _via_proxy). Letting uvicorn rewrite request.client from a
        # spoofable X-Forwarded-For would bypass that gate.
        uvicorn.run(app, host=host, port=args.port, timeout_keep_alive=65,
                    proxy_headers=False)


if __name__ == "__main__":
    main()
