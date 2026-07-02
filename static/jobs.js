// Global job tracker. A single active job (server-serialized) is polled here
// regardless of which view is mounted, so operations keep running and reporting
// as the user browses. Views render its log/progress via mountJobPane(); the
// header indicator and edit-blocking subscribe via subscribeJob()/isBusy().
import { jget, jpost, toast, escapeHtml, escapeAttr, promptModal, openModal, closeModal } from "./util.js";

let active = null;   // { id, kind }
let snap = null;     // last /api/jobs/{id} payload
let polling = false;
let answering = false;
const subs = new Set();

const LABELS = { sync: "Sync", standardize: "Standardize", import: "Import", rip: "Rip CD" };
export function jobLabel(kind) { return LABELS[kind] || "Operation"; }

function view() {
  return (active && snap) ? { id: active.id, kind: active.kind, ...snap } : null;
}
function notify() { const v = view(); for (const fn of subs) fn(v); }

export function subscribeJob(fn) { subs.add(fn); fn(view()); return () => subs.delete(fn); }

// Keep a button disabled while any job is active (so a second operation can't
// start mid-import). `alsoDisabled()` adds a view-specific condition. Self-cleans
// once the button leaves the DOM.
export function disableWhileBusy(btn, alsoDisabled = () => false) {
  const unsub = subscribeJob(() => {
    if (!btn.isConnected) { unsub(); return; }
    btn.disabled = isBusy() || alsoDisabled();
  });
  return unsub;
}
export function getActiveJob() { return view(); }
export function isBusy() { return !!(snap && (snap.state === "running" || snap.state === "waiting")); }
export function isJobKind(kind) { return isBusy() && active && active.kind === kind; }

export async function startJob(kind, params) {
  const r = await jpost("/api/jobs", { kind, ...params });   // throws on 409
  active = { id: r.job_id, kind };
  snap = { state: "running", log: [], progress: "", prompt: null, result: {}, error: "" };
  notify();
  pump();
  return active.id;
}

export async function cancelJob() {
  if (active) await jpost(`/api/jobs/${active.id}/cancel`, {}).catch(() => {});
}

// Drop a finished job from view (only when idle), e.g. "back to the form".
export function dismissJob() {
  if (!isBusy()) { active = null; snap = null; notify(); }
}

// Resume tracking a job that was already running when the page loaded.
export async function initJobs() {
  try {
    const data = await jget("/api/jobs/active");
    if (data.active) {
      active = { id: data.active.id, kind: data.active.kind };
      snap = data.active;
      notify();
      pump();
    }
  } catch { /* ignore */ }
}

async function pump() {
  if (polling) return;
  polling = true;
  while (active) {
    let j;
    try { j = await jget(`/api/jobs/${active.id}`); }
    catch { await sleep(800); continue; }
    if (!active) break;
    snap = j;
    notify();
    if (j.state === "waiting" && !answering) {
      answering = true;
      const value = await answerPrompt(j.prompt);
      try { await jpost(`/api/jobs/${active.id}/respond`, { value }); }
      catch (e) { toast(e.message, true); }
      answering = false;
      continue;
    }
    if (j.state === "done" || j.state === "error") break;  // leave snapshot for views
    await sleep(500);
  }
  polling = false;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Render the active job's progress + log into a container, kept live via a
// subscription that self-removes once the container leaves the DOM.
export function mountJobPane(container, { kind = null, log = true, collapsible = false, onDone } = {}) {
  let finished = false;
  let unsub = null;
  const handler = (job) => {
    if (!container.isConnected) { if (unsub) unsub(); return; }
    if (!job || (kind && job.kind !== kind)) { container.innerHTML = ""; return; }
    renderJobInto(container, job, log, collapsible);
    if ((job.state === "done" || job.state === "error") && !finished) {
      finished = true;
      if (onDone) onDone(job);
    }
  };
  unsub = subscribeJob(handler);
  return unsub;
}

function renderJobInto(container, job, showLog = true, collapsible = false) {
  const running = job.state === "running" || job.state === "waiting";
  const head = running ? jobLabel(job.kind) + "…"
             : job.state === "error" ? "Error"
             : job.cancelled ? "Cancelled" : "Finished";
  const bar = !running ? ""
    : job.percent != null
      ? `<div class="jobbar det"><div style="width:${job.percent}%"></div></div>`
      : `<div class="jobbar"><div></div></div>`;
  // We rewrite innerHTML on every poll, so a <details> would snap shut each tick —
  // carry the user's open/closed choice across re-renders.
  const wasOpen = !!container.querySelector("details.joblog[open]");
  const logHtml = escapeHtml((job.log || []).join("\n"));
  const logBlock = collapsible
    ? `<details class="joblog"${wasOpen ? " open" : ""}><summary>Show details</summary>
         <div class="log">${logHtml}</div></details>`
    : showLog ? `<div class="log">${logHtml}</div>` : ``;
  container.innerHTML = `
    <div class="field" style="justify-content:space-between;margin:0 0 6px">
      <strong class="${job.state === "error" ? "err" : running || job.cancelled ? "warn" : "ok"}">${head}</strong>
      ${running ? `<button class="btn danger" data-cancel>Cancel</button>` : ``}
    </div>
    ${bar}
    <div class="muted" style="margin:4px 0">${escapeHtml(job.progress || "")}</div>
    ${job.state === "error" && job.error
      ? `<div class="err" style="margin:4px 0">${escapeHtml(job.error)}</div>` : ``}
    ${logBlock}`;
  const cancel = container.querySelector("[data-cancel]");
  if (cancel) cancel.onclick = () => cancelJob();
  const logEl = container.querySelector(".log");
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
}

// ── Prompt handling (text / choice / editable import preview) ─────────────────

// A view (e.g. Import) can supply a richer inline renderer for the preview prompt.
// It returns {proceed, entries} | {proceed:false}, or null to defer to the modal.
let previewRenderer = null;
export function setPreviewRenderer(fn) { previewRenderer = fn; }

async function answerPrompt(p) {
  if (p.kind === "preview") {
    if (previewRenderer) {
      const r = await previewRenderer(p);
      if (r) return r;
    }
    return await previewModal(p);
  }
  if (p.kind === "choice") {
    const k = await promptModal({ title: p.prompt, kind: "choice", options: p.options });
    return k ?? "";
  }
  const v = await promptModal({ title: (p.prompt || "").trim() || "Enter value", kind: "text" });
  return v ?? "";
}

function previewModal(p) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; closeModal(); resolve(v); } };
    const rows = p.entries.map(e => `
      <tr data-i="${e.i}">
        <td class="muted">${escapeHtml(e.name)}</td>
        <td><input data-f="track" value="${escapeAttr(e.track)}" style="width:60px"></td>
        <td><input data-f="title" value="${escapeAttr(e.title)}" style="width:100%"></td>
        <td><input data-f="artist" value="${escapeAttr(e.artist)}" style="width:100%"></td>
        <td><input data-f="album" value="${escapeAttr(e.album)}" style="width:100%"></td>
        <td><input data-f="genre" value="${escapeAttr(e.genre || "")}" style="width:100%"></td>
        <td><input data-f="year" value="${escapeAttr(e.year)}" style="width:60px"></td>
      </tr>`).join("");
    openModal(`
      <h3>Import preview — ${p.entries.length} track${p.entries.length === 1 ? "" : "s"}
        ${p.has_lossless ? `<span class="pill">lossless present</span>` : ""}</h3>
      <p class="muted">Edit any field before importing. Lossless bitrate is chosen via the next prompt.</p>
      <div style="max-height:55vh;overflow:auto">
      <table><thead><tr><th>File</th><th>#</th><th>Title</th><th>Artist</th><th>Album</th><th>Genre</th><th>Year</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <div class="row">
        <button class="btn" data-cancel>Cancel import</button>
        <button class="btn primary" data-ok>Import</button>
      </div>`,
      (box) => {
        const collect = () => [...box.querySelectorAll("tr[data-i]")].map(tr => {
          const row = { i: Number(tr.dataset.i) };
          tr.querySelectorAll("input[data-f]").forEach(inp => row[inp.dataset.f] = inp.value);
          return row;
        });
        box.querySelector("[data-ok]").onclick = () => finish({ proceed: true, entries: collect() });
        box.querySelector("[data-cancel]").onclick = () => finish({ proceed: false });
      });
  });
}
