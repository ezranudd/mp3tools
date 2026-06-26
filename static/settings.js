// Settings view: full editor for everything in settings.DEFAULTS.
import { jget, jpost, toast, escapeHtml, escapeAttr } from "./util.js";

const BOOLS = [
  ["enforce_artist_equals_album_artist", "Enforce Artist = Album Artist"],
  ["replace_brackets_with_parentheses", "Replace [] with () in titles"],
  ["preserve_replay_gain", "Preserve replay gain tags"],
  ["preserve_tcmp", "Preserve iTunes compilation flag"],
  ["preserve_disc_numbers", "Preserve disc numbers on merge"],
  ["eject_cd_after_import", "Eject disc after CD import"],
  ["fetch_art_online", "Fetch missing art during Standardize"],
];
const SOURCES = ["itunes", "musicbrainz", "theaudiodb", "discogs"];

let container, cfg;

export async function show(el) {
  container = el;
  el.innerHTML = `<div class="page"><h2>Settings</h2><p class="muted">Loading…</p></div>`;
  try {
    cfg = await jget("/api/settings");
    render();
  } catch (e) {
    el.querySelector(".page").innerHTML = `<h2>Settings</h2><p class="err">${escapeHtml(e.message)}</p>`;
  }
}

function checkbox(key, label, checked) {
  return `<div class="field"><label>${escapeHtml(label)}</label>
    <input type="checkbox" class="toggle" data-bool="${escapeAttr(key)}" ${checked ? "checked" : ""}></div>`;
}

function render() {
  const ca = cfg.cover_art || "folder";
  const srcs = cfg.art_sources || {};
  container.innerHTML = `<div class="page">
    <h2>Settings</h2>

    <div class="card"><h4>Cover art</h4>
      ${["folder", "embed", "both"].map(v =>
        `<label class="field" style="cursor:pointer"><input type="radio" name="cover_art" value="${v}" ${v === ca ? "checked" : ""}> ${v}</label>`).join("")}
      <div class="field"><label>Max embed size (px, 0 = no resize)</label>
        <input type="number" id="ca_size" value="${escapeAttr(cfg.cover_art_embed_size ?? 500)}" style="width:100px"></div>
    </div>

    <div class="card"><h4>Standardize / Import</h4>
      ${BOOLS.map(([k, l]) => checkbox(k, l, cfg[k])).join("")}
    </div>

    <div class="card"><h4>Artwork sources</h4>
      ${SOURCES.map(s => `<div class="field"><label>${s}</label>
        <input type="checkbox" class="toggle" data-src="${s}" ${srcs[s] ? "checked" : ""}></div>`).join("")}
      <div class="field"><label>TheAudioDB API key</label>
        <input id="adb_key" value="${escapeAttr(cfg.theaudiodb_api_key || "")}" style="width:240px"></div>
      <div class="field"><label>Discogs token</label>
        <input id="dgs_token" value="${escapeAttr(cfg.discogs_token || "")}" style="width:240px"></div>
    </div>

    <div class="row" style="justify-content:flex-start">
      <button class="btn primary" id="saveBtn">Save</button>
      <button class="btn" id="reloadBtn">Reload</button>
    </div>
  </div>`;

  container.querySelector("#saveBtn").onclick = save;
  container.querySelector("#reloadBtn").onclick = () => show(container);
}

async function save() {
  const body = {
    cover_art: container.querySelector('input[name="cover_art"]:checked').value,
    cover_art_embed_size: Math.max(0, parseInt(container.querySelector("#ca_size").value, 10) || 0),
    art_sources: {},
    theaudiodb_api_key: container.querySelector("#adb_key").value.trim(),
    discogs_token: container.querySelector("#dgs_token").value.trim(),
  };
  container.querySelectorAll("[data-bool]").forEach(c => body[c.dataset.bool] = c.checked);
  container.querySelectorAll("[data-src]").forEach(c => body.art_sources[c.dataset.src] = c.checked);
  try {
    cfg = await jpost("/api/settings", body);
    toast("Settings saved.");
  } catch (e) { toast(e.message, true); }
}
