// Sync view: pick a device, choose artists/albums, preview the plan, then mirror.
import { jget, jpost, toast, escapeHtml, escapeAttr } from "./util.js";
import { startJob, mountJobPane, disableWhileBusy } from "./jobs.js";

// Device-type icons (inline SVG, currentColor) — matches d.type from the server.
const _svg = (body) => `<svg viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
const DEVICE_ICON = {
  drive: _svg(`<rect x="3" y="6" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="17" cy="12" r="1.6"/>`),
  usb: _svg(`<rect x="9" y="2" width="6" height="14" rx="1" fill="none" stroke="currentColor" stroke-width="2"/><path stroke="currentColor" stroke-width="2" d="M12 16v6M9 6h6"/>`),
  sd: _svg(`<path fill="none" stroke="currentColor" stroke-width="2" d="M7 3h7l4 4v14H7z"/><path stroke="currentColor" stroke-width="1.6" d="M10 6v3M13 6v3M16 8v1"/>`),
  generic: _svg(`<path fill="none" stroke="currentColor" stroke-width="2" d="M3 7h6l2 2h10v10H3z"/>`),
};
const deviceIcon = (type) => DEVICE_ICON[type] || DEVICE_ICON.generic;

let el;
let device = "";
let artists = [];   // {path,name,size_h,status,mode,expanded,loaded,albums:[{path,name,size_h,status,selected}]}

export function show(container) {
  el = container;
  el.innerHTML = `<div class="page">
    <h2>Sync</h2>
    <p class="muted">Mirror selected artists/albums from the library to a device
      (USB stick, SD card, phone…). Whole-artist selections also prune albums that
      were removed from the library; partial selections leave other device albums
      untouched.</p>
    <div class="field" style="margin-top:10px">
      <label style="min-width:auto">Detected devices</label>
      <button class="btn" id="rescanBtn">Rescan</button>
    </div>
    <div id="deviceList" class="devicelist"></div>
    <div class="field" style="margin-top:6px">
      <label style="min-width:auto">Or enter a path</label>
      <input id="devPath" placeholder="/run/media/you/MUSIC" style="width:340px"
             value="${escapeAttr(device)}">
      <button class="btn primary" id="loadBtn">Load</button>
    </div>
    <div class="field">
      <label style="min-width:auto"><input type="checkbox" id="dryRun"> Dry run (preview only)</label>
    </div>
    <div id="syncBody"></div>
  </div>`;
  el.querySelector("#loadBtn").onclick = () => load();
  el.querySelector("#rescanBtn").onclick = loadDevices;
  el.querySelector("#devPath").onkeydown = e => { if (e.key === "Enter") load(); };
  loadDevices();
  if (artists.length) renderBody();
}

async function loadDevices() {
  const host = el.querySelector("#deviceList");
  host.innerHTML = `<span class="muted">Scanning…</span>`;
  try {
    const data = await jget("/api/sync/devices");
    if (!data.devices.length) {
      host.innerHTML = `<span class="muted">No removable devices detected — enter a path below.</span>`;
      return;
    }
    host.innerHTML = "";
    for (const d of data.devices) {
      const b = document.createElement("button");
      b.className = "btn devbtn" + (d.path === device ? " primary" : "");
      b.innerHTML = `${deviceIcon(d.type)}<span>${escapeHtml(d.name)}</span>
        <span class="muted">${escapeHtml(d.free_h)} free / ${escapeHtml(d.total_h)}</span>`;
      b.title = `${d.type} · ${d.path}`;
      b.onclick = () => { el.querySelector("#devPath").value = d.path; load(d.path); };
      host.appendChild(b);
    }
  } catch (e) {
    host.innerHTML = `<span class="err">${escapeHtml(e.message)}</span>`;
  }
}

async function load(devPath) {
  device = (devPath || el.querySelector("#devPath").value).trim();
  if (!device) { toast("Enter a device folder.", true); return; }
  el.querySelector("#devPath").value = device;
  const body = el.querySelector("#syncBody");
  body.innerHTML = `<p class="muted">Scanning library and device…</p>`;
  try {
    const data = await jget("/api/sync/artists?device=" + encodeURIComponent(device));
    device = data.device;
    artists = data.artists.map(a => ({
      ...a, mode: "none", expanded: false, loaded: false, albums: [],
    }));
    el.querySelectorAll("#deviceList .devbtn").forEach(b =>
      b.classList.toggle("primary", b.title === device));
    renderBody();
  } catch (e) {
    body.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
  }
}

function renderBody() {
  const body = el.querySelector("#syncBody");
  body.innerHTML = `
    <div class="field" style="gap:8px">
      <button class="btn" id="selAll">Select all</button>
      <button class="btn" id="selNone">Select none</button>
      <span class="muted" id="selSummary" style="margin-left:8px"></span>
    </div>
    <div id="syncList" class="card" style="max-height:48vh;overflow:auto;padding:6px 10px"></div>
    <div class="field" style="gap:8px;margin-top:10px">
      <button class="btn" id="previewBtn">Preview plan</button>
      <button class="btn primary" id="syncBtn">Sync</button>
    </div>
    <div id="planPanel"></div>
    <div id="jobArea" style="margin-top:12px"></div>`;
  body.querySelector("#selAll").onclick = () => { selectAll(true); };
  body.querySelector("#selNone").onclick = () => { selectAll(false); };
  body.querySelector("#previewBtn").onclick = previewPlan;
  body.querySelector("#syncBtn").onclick = runSync;
  disableWhileBusy(body.querySelector("#syncBtn"));   // no sync while a job runs
  mountJobPane(body.querySelector("#jobArea"), { kind: "sync", log: false });
  renderList();
}

function statusClass(s) {
  if (!s || s === "not on device") return "muted";
  if (s === "synced") return "ok";
  return "warn";
}

function renderList() {
  const host = el.querySelector("#syncList");
  if (!artists.length) { host.innerHTML = `<p class="muted">No artists in library.</p>`; return; }
  let html = "";
  artists.forEach((a, ai) => {
    html += `<div class="syncrow" style="display:flex;align-items:center;gap:8px;padding:2px 0">
      <input type="checkbox" data-art="${ai}">
      <span class="caret" data-exp="${ai}" style="cursor:pointer;width:14px">${a.expanded ? "▾" : "▸"}</span>
      <span style="flex:1;font-weight:600">${escapeHtml(a.name)}</span>
      <span class="muted" style="width:80px;text-align:right">${escapeHtml(a.size_h)}</span>
      <span class="${statusClass(a.status)}" style="width:130px">${escapeHtml(a.status || "")}</span>
    </div>`;
    if (a.expanded) {
      if (!a.albums.length) {
        html += `<div class="muted" style="padding:2px 0 2px 38px">No album subfolders (loose tracks).</div>`;
      }
      a.albums.forEach((alb, bi) => {
        html += `<div class="syncrow" style="display:flex;align-items:center;gap:8px;padding:2px 0 2px 30px">
          <input type="checkbox" data-art="${ai}" data-alb="${bi}" ${alb.selected ? "checked" : ""}>
          <span style="flex:1">${escapeHtml(alb.name)}</span>
          <span class="muted" style="width:80px;text-align:right">${escapeHtml(alb.size_h)}</span>
          <span class="${statusClass(alb.status)}" style="width:130px">${escapeHtml(alb.status || "")}</span>
        </div>`;
      });
    }
  });
  host.innerHTML = html;

  // Wire artist checkboxes (with tri-state) and expand carets.
  host.querySelectorAll("input[data-art]").forEach(cb => {
    const ai = +cb.dataset.art;
    if (cb.dataset.alb === undefined) {
      const a = artists[ai];
      cb.checked = a.mode === "all";
      cb.indeterminate = a.mode === "some";
      cb.onchange = () => toggleArtist(ai);
    } else {
      cb.onchange = () => toggleAlbum(ai, +cb.dataset.alb);
    }
  });
  host.querySelectorAll("[data-exp]").forEach(c => c.onclick = () => expand(+c.dataset.exp));
  updateSummary();
}

function updateSummary() {
  const nArtists = artists.filter(a => a.mode !== "none").length;
  const nAlbums = artists.reduce((n, a) =>
    n + (a.mode === "some" ? a.albums.filter(x => x.selected).length
       : a.mode === "all" ? (a.albums.length || 1) : 0), 0);
  const s = el.querySelector("#selSummary");
  if (s) s.textContent = `${nArtists} artist${nArtists === 1 ? "" : "s"}, ${nAlbums} album${nAlbums === 1 ? "" : "s"} selected`;
}

async function expand(ai) {
  const a = artists[ai];
  a.expanded = !a.expanded;
  if (a.expanded && !a.loaded) {
    try {
      const data = await jget(`/api/sync/albums?artist=${encodeURIComponent(a.path)}&device=${encodeURIComponent(device)}`);
      a.albums = data.albums.map(x => ({ ...x, selected: a.mode === "all" }));
      a.loaded = true;
    } catch (e) { toast(e.message, true); a.expanded = false; }
  }
  renderList();
}

function toggleArtist(ai) {
  const a = artists[ai];
  const on = a.mode !== "all";
  a.mode = on ? "all" : "none";
  if (a.loaded) a.albums.forEach(x => x.selected = on);
  renderList();
}

function toggleAlbum(ai, bi) {
  const a = artists[ai];
  a.albums[bi].selected = !a.albums[bi].selected;
  const sel = a.albums.filter(x => x.selected).length;
  a.mode = sel === 0 ? "none" : sel === a.albums.length ? "all" : "some";
  renderList();
}

function selectAll(on) {
  artists.forEach(a => {
    a.mode = on ? "all" : "none";
    if (a.loaded) a.albums.forEach(x => x.selected = on);
  });
  renderList();
}

function buildSelection() {
  const sel = {};
  for (const a of artists) {
    if (a.mode === "none") continue;
    if (a.mode === "all") sel[a.path] = "all";
    else sel[a.path] = a.albums.filter(x => x.selected).map(x => x.path);
  }
  return sel;
}

async function previewPlan() {
  const selection = buildSelection();
  if (!Object.keys(selection).length) { toast("Nothing selected.", true); return; }
  const panel = el.querySelector("#planPanel");
  panel.innerHTML = `<p class="muted">Building plan…</p>`;
  try {
    const p = await jpost("/api/sync/plan", { device, selection });
    panel.innerHTML = `<div class="card" style="margin-top:10px">
      <div>Files to copy: <b>${p.copy_files}</b> (${escapeHtml(p.copy_h)})</div>
      <div>Files to delete: <b>${p.remove_files}</b> (${escapeHtml(p.remove_h)})</div>
      <div>Free space: ${escapeHtml(p.free_h)} · net needed: ${escapeHtml(p.net_h)}</div>
      ${p.enough_space ? "" : `<div class="err">Not enough free space.</div>`}
    </div>`;
  } catch (e) { panel.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`; }
}

async function runSync() {
  const selection = buildSelection();
  if (!Object.keys(selection).length) { toast("Nothing selected.", true); return; }
  const dry_run = el.querySelector("#dryRun").checked;
  try { await startJob("sync", { device, selection, dry_run }); }
  catch (e) { toast(e.message, true); }
}
