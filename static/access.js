// Access view (owner-only): manage remote internet access — set/rotate the
// shared password and approve / rename / block / revoke devices. Backed by the
// owner-only /api/admin/access endpoints; mirrors the devices.js polling shape.
import { jget, jpost, toast, escapeHtml, escapeAttr, promptModal } from "./util.js";

const POLL_MS = 5000;
let container;
let timer = 0;

export function show(el) {
  container = el;
  el.innerHTML = `<div class="page">
    <h2>Remote access</h2>
    <p class="muted">Approve devices and manage the shared access password for
      internet access. Approved devices are read-only (browse + play).</p>
    <div id="accStatus"></div>
    <h3 style="margin-top:20px">Devices</h3>
    <div id="accDevices"><p class="muted">Loading…</p></div>
  </div>`;
  startPolling();
}

export function beforeLeave() { stopPolling(); return true; }

function startPolling() { stopPolling(); poll(); timer = setInterval(poll, POLL_MS); }
function stopPolling() { if (timer) { clearInterval(timer); timer = 0; } }

async function poll() {
  let data;
  try {
    data = await jget("/api/admin/access");
  } catch (e) {
    const el = container && container.querySelector("#accDevices");
    if (el && !el.querySelector(".card")) el.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!container) return;
  renderStatus(data);
  renderDevices(data.devices || []);
}

function renderStatus(data) {
  const el = container.querySelector("#accStatus");
  if (!el) return;
  const pw = data.password_set
    ? `<span class="pill">password set</span>`
    : `<span class="pill" style="border-color:var(--err);color:var(--err)">no password</span>`;
  const remote = data.remote_enabled
    ? `<span class="pill">remote enabled</span>`
    : `<span class="muted">remote serving off (start with <code>--remote</code>)</span>`;
  el.innerHTML = `<div class="card">
    <div class="row" style="justify-content:space-between;align-items:center">
      <div>${pw} &nbsp; ${remote}</div>
      <button class="btn" id="setpw">${data.password_set ? "Change password" : "Set password"}</button>
    </div>
  </div>`;
  el.querySelector("#setpw").onclick = setPassword;
}

async function setPassword() {
  const pw = await promptModal({ title: "Set access password (min 8 chars)", kind: "text" });
  if (pw === null) return;
  if (pw.length < 8) { toast("Password must be at least 8 characters", true); return; }
  try {
    await jpost("/api/admin/access/password", { password: pw });
    toast("Password updated — all remote sessions were signed out");
    poll();
  } catch (e) { toast(e.message, true); }
}

function fmtWhen(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function stateBadge(state, online) {
  if (state === "pending") return `<span class="pill" style="border-color:var(--accent)">pending</span>`;
  if (state === "blocked") return `<span class="pill" style="border-color:var(--err);color:var(--err)">blocked</span>`;
  return online ? `<span class="pill">online</span>` : `<span class="pill">approved</span>`;
}

function deviceCard(d) {
  const name = d.name || "Unnamed device";
  const loc = d.location && d.location.label ? ` · 📍 ${escapeHtml(d.location.label)}` : "";
  const seen = d.online ? "online now" : (d.last_seen ? `last seen ${fmtWhen(d.last_seen)}` : `added ${fmtWhen(d.first_seen)}`);
  const actions = [];
  if (d.state !== "approved") actions.push(`<button class="btn primary" data-act="approve">Approve</button>`);
  actions.push(`<button class="btn" data-act="rename">Rename</button>`);
  if (d.state !== "blocked") actions.push(`<button class="btn" data-act="block">Block</button>`);
  actions.push(`<button class="btn" data-act="revoke">Forget</button>`);
  return `<div class="card" data-cid="${escapeAttr(d.cid)}">
    <div class="row" style="justify-content:space-between;align-items:flex-start;gap:10px">
      <div>
        <h4 style="margin:0">${escapeHtml(name)} ${stateBadge(d.state, d.online)}</h4>
        <div class="muted">${escapeHtml(d.ip || "")}${loc}</div>
        <div class="muted">${escapeHtml(seen)}</div>
      </div>
      <div class="row" style="flex-wrap:wrap;gap:6px;justify-content:flex-end">${actions.join("")}</div>
    </div>
  </div>`;
}

function renderDevices(devices) {
  const el = container.querySelector("#accDevices");
  if (!el) return;
  if (!devices.length) {
    el.innerHTML = `<p class="muted">No devices yet. When someone signs in with the
      access password, they'll appear here for approval.</p>`;
    return;
  }
  el.innerHTML = devices.map(deviceCard).join("");
  el.querySelectorAll(".card[data-cid]").forEach(card => {
    const cid = card.dataset.cid;
    card.querySelectorAll("button[data-act]").forEach(btn => {
      btn.onclick = () => act(btn.dataset.act, cid);
    });
  });
}

async function act(action, cid) {
  try {
    if (action === "rename") {
      const name = await promptModal({ title: "Device name", kind: "text" });
      if (name === null) return;
      await jpost("/api/admin/access/rename", { cid, name });
    } else if (action === "revoke") {
      await jpost("/api/admin/access/revoke", { cid });
    } else {
      await jpost(`/api/admin/access/${action}`, { cid });
    }
    poll();
  } catch (e) { toast(e.message, true); }
}
