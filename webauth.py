"""
Web UI authentication for remote (internet) access — stdlib only.

Self-contained credential/session state for server.py's remote-access mode.
State lives under {library_root}/.mp3tools/ and is **never served by any HTTP
route**:

  - auth.json     : access-password hash (scrypt) + per-device whitelist
  - sessions.json : opaque session tokens → {cid, ip, created, expires}

Trust/role enforcement lives in server.py (`_access_gate`, `_is_owner`); this
module only owns the shared access password, the device approval whitelist, the
session table, and login rate-limiting. Everything is lock-guarded and
fail-soft: an unreadable/missing file behaves as "no password set / no devices".

Design notes:
  - One shared *access password* gates the door (scrypt + per-password salt).
  - Each browser carries a stable `cid`; approval is per-device. A device's
    role is derived **live** from its current whitelist state, so approve/block
    takes effect on the device's very next request.
  - Setting/rotating the password clears every session (forces re-login).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

import settings as settings_mod

# scrypt cost parameters (RFC 7914). ~16 MB, well within OpenSSL's default cap.
_SCRYPT = {"n": 16384, "r": 8, "p": 1}
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_DKLEN = 32

SESSION_TTL = 30 * 24 * 3600        # 30 days
_COOKIE_NAME = "mp3session"

# Login rate limiting (per real client IP, in-memory).
_RL_FREE = 5                        # failures allowed before lockout kicks in
_RL_CAP = 300                       # max lockout seconds
_RL_WINDOW = 900                    # forget failures after this many idle seconds

_ROOT: Path | None = None
_LOCK = threading.RLock()
_auth: dict | None = None           # lazily loaded auth.json contents
_sessions: dict | None = None       # lazily loaded sessions.json contents
_rl: dict[str, dict] = {}           # ip → {"fails": int, "last": ts}


# ── configuration / persistence ───────────────────────────────────────────────

def configure(library_root) -> None:
    """Point the module at a library root (call before serving)."""
    global _ROOT, _auth, _sessions
    _ROOT = Path(library_root).expanduser().resolve()
    _auth = None
    _sessions = None


def _auth_file() -> Path:
    return settings_mod.settings_dir(_ROOT) / "auth.json"


def _sessions_file() -> Path:
    return settings_mod.settings_dir(_ROOT) / "sessions.json"


def _load_json(path: Path, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def _save_json(path: Path, data: dict) -> None:
    """Best-effort persist. Caller holds _LOCK."""
    try:
        d = settings_mod.settings_dir(_ROOT)
        d.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp.replace(path)            # atomic swap
    except Exception:
        pass


def _auth_dict() -> dict:
    global _auth
    if _auth is None:
        _auth = _load_json(_auth_file(), {"password": None, "devices": {}})
        _auth.setdefault("password", None)
        _auth.setdefault("devices", {})
    return _auth


def _sessions_dict() -> dict:
    global _sessions
    if _sessions is None:
        _sessions = _load_json(_sessions_file(), {})
    return _sessions


# ── password ──────────────────────────────────────────────────────────────────

def _derive(pw: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                          n=params["n"], r=params["r"], p=params["p"],
                          maxmem=_SCRYPT_MAXMEM, dklen=_DKLEN)


def set_password(pw: str) -> None:
    """Set/rotate the shared access password and invalidate all sessions."""
    if not pw:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    rec = {"algo": "scrypt", **_SCRYPT,
           "salt": base64.b64encode(salt).decode(),
           "hash": base64.b64encode(_derive(pw, salt, _SCRYPT)).decode()}
    with _LOCK:
        a = _auth_dict()
        a["password"] = rec
        _save_json(_auth_file(), a)
        # Rotating the credential logs everyone out.
        global _sessions
        _sessions = {}
        _save_json(_sessions_file(), _sessions)


def password_set() -> bool:
    with _LOCK:
        return bool(_auth_dict().get("password"))


def verify_password(pw: str) -> bool:
    """Constant-time verify against the stored hash. False if none is set."""
    with _LOCK:
        rec = _auth_dict().get("password")
    if not rec:
        return False
    try:
        salt = base64.b64decode(rec["salt"])
        expected = base64.b64decode(rec["hash"])
        params = {"n": rec["n"], "r": rec["r"], "p": rec["p"]}
    except Exception:
        return False
    return hmac.compare_digest(_derive(pw, salt, params), expected)


# ── devices (whitelist) ───────────────────────────────────────────────────────

def _device(cid: str) -> dict | None:
    return _auth_dict()["devices"].get(cid)


def device_state(cid: str) -> str | None:
    """'approved' | 'pending' | 'blocked' | None (unknown)."""
    with _LOCK:
        d = _device(cid)
        return d["state"] if d else None


def register_pending(cid: str, ip: str, name: str = "") -> str:
    """Ensure *cid* is tracked (new → pending) and refresh its last-seen ip.
    Returns the device's current state."""
    now = time.time()
    with _LOCK:
        a = _auth_dict()
        d = a["devices"].get(cid)
        if d is None:
            d = {"state": "pending", "name": name or "", "ip": ip,
                 "first_seen": now, "approved_at": None}
            a["devices"][cid] = d
        else:
            d["ip"] = ip
            if name and not d.get("name"):
                d["name"] = name
        _save_json(_auth_file(), a)
        return d["state"]


def approve(cid: str, name: str = "") -> None:
    with _LOCK:
        a = _auth_dict()
        d = a["devices"].setdefault(cid, {"first_seen": time.time(), "ip": ""})
        d["state"] = "approved"
        d["approved_at"] = time.time()
        if name:
            d["name"] = name
        _save_json(_auth_file(), a)


def block(cid: str) -> None:
    with _LOCK:
        a = _auth_dict()
        d = a["devices"].get(cid)
        if d:
            d["state"] = "blocked"
            _save_json(_auth_file(), a)
        _destroy_sessions_for(cid)


def revoke(cid: str) -> None:
    """Forget a device entirely and kill its sessions."""
    with _LOCK:
        a = _auth_dict()
        a["devices"].pop(cid, None)
        _save_json(_auth_file(), a)
        _destroy_sessions_for(cid)


def rename(cid: str, name: str) -> None:
    with _LOCK:
        d = _device(cid)
        if d:
            d["name"] = name
            _save_json(_auth_file(), _auth_dict())


def list_devices() -> list[dict]:
    with _LOCK:
        return [{"cid": cid, **d} for cid, d in _auth_dict()["devices"].items()]


# ── sessions ──────────────────────────────────────────────────────────────────

COOKIE_NAME = _COOKIE_NAME


def create_session(cid: str, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        s = _sessions_dict()
        s[token] = {"cid": cid, "ip": ip, "created": now, "expires": now + SESSION_TTL}
        _save_json(_sessions_file(), s)
    return token


def _purge_expired(s: dict) -> bool:
    now = time.time()
    dead = [t for t, v in s.items() if v.get("expires", 0) < now]
    for t in dead:
        del s[t]
    return bool(dead)


def session_cid(token: str | None) -> str | None:
    """The cid behind a valid, unexpired session token (else None)."""
    if not token:
        return None
    with _LOCK:
        s = _sessions_dict()
        if _purge_expired(s):
            _save_json(_sessions_file(), s)
        rec = s.get(token)
        return rec["cid"] if rec else None


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _LOCK:
        s = _sessions_dict()
        if s.pop(token, None) is not None:
            _save_json(_sessions_file(), s)


def _destroy_sessions_for(cid: str) -> None:
    """Drop every session belonging to *cid*. Caller holds _LOCK."""
    s = _sessions_dict()
    dead = [t for t, v in s.items() if v.get("cid") == cid]
    for t in dead:
        del s[t]
    if dead:
        _save_json(_sessions_file(), s)


def resolve(token: str | None) -> str:
    """Map a session token to a role token for server.py:
    'anonymous' (no/expired session or unknown device),
    'pending' (device awaiting approval),
    'member' (approved), 'blocked' (denied)."""
    cid = session_cid(token)
    if not cid:
        return "anonymous"
    state = device_state(cid)
    if state == "approved":
        return "member"
    if state == "blocked":
        return "blocked"
    return "pending"


# ── login rate limiting (per real client IP) ──────────────────────────────────

def login_allowed(ip: str) -> tuple[bool, int]:
    """(allowed, retry_after_seconds). Locks an IP out with exponential backoff
    once it passes _RL_FREE recent failures."""
    now = time.time()
    with _LOCK:
        rec = _rl.get(ip)
        if not rec or now - rec["last"] > _RL_WINDOW:
            return True, 0
        fails = rec["fails"]
        if fails < _RL_FREE:
            return True, 0
        # Exponential backoff once past the free attempts (2s, 4s, 8s … capped).
        lock = min(2 ** (fails - _RL_FREE + 1), _RL_CAP)
        remaining = max(0, int(round(rec["last"] + lock - now)))
        if remaining > 0:
            return False, remaining
        return True, 0


def note_failure(ip: str) -> None:
    now = time.time()
    with _LOCK:
        rec = _rl.get(ip)
        if not rec or now - rec["last"] > _RL_WINDOW:
            rec = {"fails": 0, "last": now}
        rec["fails"] += 1
        rec["last"] = now
        _rl[ip] = rec


def note_success(ip: str) -> None:
    with _LOCK:
        _rl.pop(ip, None)
