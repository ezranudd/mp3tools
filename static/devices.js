// Devices view (owner-only): connected clients (what each is playing + where)
// and a browsable history of past connections. Live data is tracked passively
// server-side from /api/track + /api/whoami; history is the durable connection
// log finalized when a session goes idle.
import { jget, escapeHtml } from "./util.js";

const POLL_MS = 4000;
let container;
let timer = 0;

export function show(el) {
  container = el;
  el.innerHTML = `<div class="page">
    <h2>Devices</h2>
    <p class="muted">Connected clients and what they're playing. Updates every few seconds.</p>
    <div id="deviceList"><p class="muted">Loading…</p></div>
    <h3 style="margin-top:22px">Connection history</h3>
    <p class="muted">Past and current sessions, newest first.</p>
    <div id="connList"><p class="muted">Loading…</p></div>
  </div>`;
  startPolling();
}

// The router calls this on the outgoing view; stop the poll loop when we leave.
export function beforeLeave() {
  stopPolling();
  return true;
}

function startPolling() {
  stopPolling();
  poll();
  timer = setInterval(poll, POLL_MS);
}

function stopPolling() {
  if (timer) { clearInterval(timer); timer = 0; }
}

async function poll() {
  // Live clients and connection history refresh together each tick.
  await Promise.all([pollClients(), pollConnections()]);
}

async function pollClients() {
  const out = () => container && container.querySelector("#deviceList");
  let data;
  try {
    data = await jget("/api/admin/clients");
  } catch (e) {
    const el = out();
    if (el && !el.querySelector(".card")) el.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
    return;
  }
  const el = out();   // the view may have been torn down mid-request
  if (el) renderClients(el, data.clients || []);
}

async function pollConnections() {
  const out = () => container && container.querySelector("#connList");
  let data;
  try {
    data = await jget("/api/admin/connections?limit=200");
  } catch (e) {
    const el = out();
    if (el && !el.querySelector(".histrow")) el.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
    return;
  }
  const el = out();
  if (el) renderConnections(el, data.connections || []);
}

// ── formatting ────────────────────────────────────────────────────────────────

function relTime(sec) {
  if (sec < 5) return "active now";
  if (sec < 60) return `active ${sec}s ago`;
  const m = Math.floor(sec / 60);
  return `active ${m}m ago`;
}

function fmtTime(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? t : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}`;
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function deviceIcon(device) { return device === "mobile" ? "📱" : "💻"; }

function metaLabel(c) {
  return [c.browser, c.os].filter(x => x && x !== "Unknown").join(" · ") || "Unknown client";
}

function locationLine(loc) {
  if (!loc || !loc.label) return "";
  const isp = loc.isp ? ` <span class="muted">· ${escapeHtml(loc.isp)}</span>` : "";
  return `<div class="muted">📍 ${escapeHtml(loc.label)}${isp}</div>`;
}

// ── live clients ──────────────────────────────────────────────────────────────

function deviceCard(c) {
  const you = c.you ? `<span class="pill">you</span>` : "";
  let np;
  if (c.now_playing) {
    const n = c.now_playing;
    const title = escapeHtml(n.title || "");
    const artist = n.artist ? ` — <span class="muted">${escapeHtml(n.artist)}</span>` : "";
    np = `<div>🎵 ${title}${artist}</div>`;
  } else {
    np = `<div class="muted">Nothing playing</div>`;
  }
  return `<div class="card">
    <h4>${deviceIcon(c.device)} ${escapeHtml(metaLabel(c))} ${you}</h4>
    <div class="muted">${escapeHtml(c.ip)} · ${escapeHtml(relTime(c.idle_seconds))}</div>
    ${locationLine(c.location)}
    <div style="margin-top:6px">${np}</div>
  </div>`;
}

function renderClients(out, clients) {
  if (!clients.length) {
    out.innerHTML = `<p class="muted">No devices connected.</p>`;
    return;
  }
  out.innerHTML = clients.map(deviceCard).join("");
}

// ── connection history ────────────────────────────────────────────────────────

function connRow(c) {
  const when = fmtTime(c.connected_at);
  const dur = c.active
    ? `<span class="pill">active</span> ${fmtDuration(c.duration_s)}`
    : fmtDuration(c.duration_s);
  const loc = c.location ? ` · 📍 ${escapeHtml(c.location)}` : "";
  return `<div class="histrow" style="display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--line,#2222)">
    <div>
      <div>${deviceIcon(c.device)} ${escapeHtml(metaLabel(c))}</div>
      <div class="muted">${escapeHtml(c.ip)}${loc}</div>
    </div>
    <div style="text-align:right;white-space:nowrap">
      <div>${escapeHtml(when)}</div>
      <div class="muted">${dur}</div>
    </div>
  </div>`;
}

function renderConnections(out, conns) {
  if (!conns.length) {
    out.innerHTML = `<p class="muted">No connections recorded yet.</p>`;
    return;
  }
  out.innerHTML = conns.map(connRow).join("");
}
