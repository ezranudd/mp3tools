// Shared helpers: fetch wrappers, escaping, toast, modal.

// fetch() never times out on its own, so a half-open socket (common after an iOS
// PWA is backgrounded or the WiFi radio sleeps) leaves a request hanging forever.
// Abort after `ms` so the dead connection is dropped and a retry can open a fresh
// one. The timer is cleared in finally so a fast response doesn't trip it.
async function _fetchWithTimeout(url, opts = {}, ms = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    // same-origin so the session cookie (remote-access mode) rides along.
    return await fetch(url, { credentials: "same-origin", ...opts, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

// In remote-access mode an expired/lost session makes the API return 401. app.js
// registers a handler here to re-show the login gate from anywhere (any jget/
// jpost), instead of each call site coping with it.
let _authHandler = null;
export function setAuthHandler(fn) { _authHandler = fn; }

// True for connection-level failures worth retrying on a fresh socket: an abort
// (our timeout) or a network TypeError. A real HTTP error response is NOT one of
// these — fetch resolves for 4xx/5xx, so those fall through to the caller as before.
function _isConnError(err) {
  return err && (err.name === "AbortError" || err instanceof TypeError);
}

async function _jsonOrThrow(r) {
  if (r.status === 401 && _authHandler) _authHandler();   // session gone → login gate
  if (!r.ok) {
    const err = new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

const _sleep = (ms) => new Promise((res) => setTimeout(res, ms));

export async function jget(url) {
  // Retry idempotent GETs across a connection drop: 8s timeout, then short backoff.
  const backoffs = [250, 600];
  for (let attempt = 0; ; attempt++) {
    try {
      return await _jsonOrThrow(await _fetchWithTimeout(url, {}, 8000));
    } catch (err) {
      if (_isConnError(err) && attempt < backoffs.length) {
        await _sleep(backoffs[attempt]);
        continue;
      }
      throw err;
    }
  }
}

export async function jpost(url, body) {
  // Mutations aren't retried (avoid double-submits), but still get a timeout so a
  // dead socket fails fast instead of hanging.
  const r = await _fetchWithTimeout(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 8000);
  return _jsonOrThrow(r);
}

// Stable per-browser id, so the server can tell devices apart (the owner's
// Devices view keys presence on this, not on the NAT-shared client IP). Sent as
// a cid= query param on /api/whoami and /api/track.
export function clientId() {
  let id = localStorage.getItem("mp3tools_cid");
  if (!id) {
    id = (crypto.randomUUID && crypto.randomUUID()) ||
      (Date.now().toString(36) + Math.random().toString(36).slice(2));
    localStorage.setItem("mp3tools_cid", id);
  }
  return id;
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

export function escapeAttr(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

let _toastTimer;
export function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => (t.className = "toast"), 2400);
}

// Make a table body's rows reorderable by dragging their `.draghandle`. Rows are
// reordered live within the tbody; onReorder() fires after a drop. Using a handle
// (rather than draggable rows) keeps any inputs in the row editable.
export function enableRowDrag(tbody, onReorder) {
  let dragging = null;
  tbody.querySelectorAll("tr").forEach(tr => {
    const handle = tr.querySelector(".draghandle");
    if (!handle) return;
    handle.draggable = true;
    handle.addEventListener("dragstart", (e) => {
      dragging = tr;
      tr.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    handle.addEventListener("dragend", () => {
      if (dragging) dragging.classList.remove("dragging");
      dragging = null;
      if (onReorder) onReorder();
    });
  });
  tbody.addEventListener("dragover", (e) => {
    if (!dragging) return;
    e.preventDefault();
    const rows = [...tbody.querySelectorAll("tr:not(.dragging)")];
    const after = rows.find(r => {
      const box = r.getBoundingClientRect();
      return e.clientY < box.top + box.height / 2;
    });
    if (after) tbody.insertBefore(dragging, after);
    else tbody.appendChild(dragging);
  });
}

const _modal = () => document.getElementById("modal");
const _modalBox = () => document.getElementById("modalBox");

// Render arbitrary HTML into the shared modal. `setup(box, close)` wires events.
export function openModal(html, setup) {
  const box = _modalBox();
  box.innerHTML = html;
  _modal().classList.add("show");
  if (setup) setup(box, closeModal);
}

export function closeModal() {
  _modal().classList.remove("show");
  _modalBox().innerHTML = "";
}

// A small text/choice prompt helper returning a Promise.
// kind="text": resolves to string|null. kind="choice": resolves to option key|null.
export function promptModal({ title, kind = "text", value = "", options = [] }) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; closeModal(); resolve(v); } };
    let body;
    if (kind === "choice") {
      body = `<div class="row" style="flex-wrap:wrap;justify-content:flex-start">` +
        options.map(o => `<button class="btn" data-k="${escapeAttr(o.key)}">${escapeHtml(o.label)}</button>`).join("") +
        `</div>`;
    } else {
      body = `<input id="pmInput" style="width:100%" value="${escapeAttr(value)}">`;
    }
    openModal(
      `<h3>${escapeHtml(title)}</h3>${body}` +
      (kind === "text" ? `<div class="row"><button class="btn" data-cancel>Cancel</button>
        <button class="btn primary" data-ok>OK</button></div>` : ""),
      (box) => {
        box.querySelectorAll("[data-k]").forEach(b =>
          b.onclick = () => finish(b.dataset.k));
        const input = box.querySelector("#pmInput");
        if (input) {
          input.focus(); input.select();
          input.onkeydown = e => {
            if (e.key === "Enter") finish(input.value);
            if (e.key === "Escape") finish(null);
          };
        }
        const ok = box.querySelector("[data-ok]");
        if (ok) ok.onclick = () => finish(input.value);
        const cancel = box.querySelector("[data-cancel]");
        if (cancel) cancel.onclick = () => finish(null);
      });
  });
}
