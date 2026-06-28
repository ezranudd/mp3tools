// Settings view: full editor for everything in settings.DEFAULTS.
import { jget, jpost, toast, escapeHtml, escapeAttr, openModal, closeModal } from "./util.js";
import { applyBackground, uploadBackground, clearBackground } from "./background.js";
import { setEnabled as setAccentEnabled, setBaseColor as setAccentBase } from "./accent.js";

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

// Accent presets: [label, hex]. "" = revert to the built-in theme accent.
// accent.js normalises each hue to a theme-appropriate shade, so these are
// just representative source colours.
const ACCENT_PRESETS = [
  ["Default", ""],
  ["Blue", "#7aa2f7"], ["Purple", "#bb9af7"], ["Rose", "#f7768e"],
  ["Red", "#ff6b6b"], ["Orange", "#ff9e64"], ["Yellow", "#e0af68"],
  ["Green", "#9ece6a"], ["Teal", "#4abfaf"], ["Cyan", "#7dcfff"],
];

let container, cfg, dirty = false;
let selectedAccent = "";   // current accent hex, "" = theme default

export async function show(el) {
  container = el;
  dirty = false;
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

// Accent-color picker: preset swatches + a native custom-color swatch.
function accentSwatches() {
  const cur = selectedAccent.toLowerCase();
  const presetHexes = ACCENT_PRESETS.map(([, h]) => h.toLowerCase());
  const isCustom = cur !== "" && !presetHexes.includes(cur);
  const customVal = /^#[0-9a-f]{6}$/i.test(selectedAccent) ? selectedAccent : "#7aa2f7";
  const presets = ACCENT_PRESETS.map(([name, hex]) => {
    const sel = hex.toLowerCase() === cur ? " sel" : "";
    const cls = hex === "" ? "accentSwatch def" : "accentSwatch";
    const style = hex ? ` style="background:${escapeAttr(hex)}"` : "";
    return `<button type="button" class="${cls}${sel}" data-accent="${escapeAttr(hex)}" title="${escapeAttr(name)}"${style}></button>`;
  }).join("");
  return `${presets}<span class="accentSwatch custom${isCustom ? " sel" : ""}" title="Custom color">
    <input type="color" id="accent_custom" value="${escapeAttr(customVal)}"></span>`;
}

// Toggle the .sel ring without rebuilding (avoids disrupting an open color picker).
function markAccentSelected() {
  const cur = selectedAccent.toLowerCase();
  const presetHexes = ACCENT_PRESETS.map(([, h]) => h.toLowerCase());
  container.querySelectorAll(".accentSwatch[data-accent]").forEach(btn =>
    btn.classList.toggle("sel", (btn.dataset.accent || "").toLowerCase() === cur));
  const custom = container.querySelector(".accentSwatch.custom");
  if (custom) custom.classList.toggle("sel", cur !== "" && !presetHexes.includes(cur));
}

function selectAccent(hex) {
  selectedAccent = hex || "";
  setAccentBase(selectedAccent);   // live preview
  dirty = true;
  markAccentSelected();
}

function wireAccent() {
  container.querySelectorAll(".accentSwatch[data-accent]").forEach(btn =>
    btn.onclick = () => selectAccent(btn.dataset.accent));
  container.querySelector("#accent_custom").oninput = (e) => selectAccent(e.target.value);
}

function render() {
  const ca = cfg.cover_art || "folder";
  const srcs = cfg.art_sources || {};
  selectedAccent = cfg.theme_accent_color || "";
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

    <div class="card"><h4>Background image</h4>
      <div class="field"><label>Image</label>
        <input type="file" id="bg_file" accept="image/*" style="display:none">
        <button class="btn" id="bg_choose">Choose image…</button>
        <button class="btn" id="bg_clear" ${cfg.background_present ? "" : "disabled"}>Clear</button>
        <span class="${cfg.background_present ? "ok" : "muted"}">${cfg.background_present ? "Current image set" : "No image"}</span></div>
      <div class="field"><label>Dim (scrim opacity)</label>
        <input type="range" id="bg_opacity" min="0" max="1" step="0.05" value="${escapeAttr(cfg.background_opacity ?? 0.4)}"></div>
      <div class="field"><label>Blur (px)</label>
        <input type="range" id="bg_blur" min="0" max="40" step="1" value="${escapeAttr(cfg.background_blur ?? 0)}"></div>
      <div class="field"><label>Fit</label>
        <select id="bg_fit">${["cover", "contain", "tile"].map(v =>
          `<option value="${v}" ${v === (cfg.background_fit || "cover") ? "selected" : ""}>${v}</option>`).join("")}</select></div>
      <div class="field"><label>Improve text readability</label>
        <input type="checkbox" id="bg_readable" ${(cfg.background_readable ?? true) ? "checked" : ""}></div>
    </div>

    <div class="card"><h4>Appearance</h4>
      <div class="field"><label>Accent color</label>
        <div class="accentSwatches">${accentSwatches()}</div></div>
      <div class="field"><label>Match theme color to album art</label>
        <input type="checkbox" class="toggle" id="accent_art" data-bool="theme_accent_from_art"
               ${cfg.theme_accent_from_art ? "checked" : ""}></div>
    </div>

    <div class="row" style="justify-content:flex-start">
      <button class="btn primary" id="saveBtn">Save</button>
      <button class="btn" id="reloadBtn">Reload</button>
    </div>
  </div>`;

  container.querySelector("#saveBtn").onclick = save;
  container.querySelector("#reloadBtn").onclick = () => show(container);
  wireBackground();
  wireAccent();
  // Live-preview the album-art accent toggle.
  container.querySelector("#accent_art").onchange =
    (e) => setAccentEnabled(e.target.checked);

  // Any control edit marks the screen dirty (background upload/clear re-render,
  // resetting this, since they persist server-side immediately).
  const markDirty = () => { dirty = true; };
  const page = container.querySelector(".page");
  page.addEventListener("input", markDirty);
  page.addEventListener("change", markDirty);
  dirty = false;
}

// Merge the live background-control values onto cfg and apply them immediately.
function liveBackground() {
  cfg.background_opacity = parseFloat(container.querySelector("#bg_opacity").value);
  cfg.background_blur = parseInt(container.querySelector("#bg_blur").value, 10);
  cfg.background_fit = container.querySelector("#bg_fit").value;
  cfg.background_readable = container.querySelector("#bg_readable").checked;
  dirty = true;
  applyBackground(cfg);
}

function wireBackground() {
  for (const id of ["#bg_opacity", "#bg_blur", "#bg_fit", "#bg_readable"])
    container.querySelector(id).oninput = liveBackground;
  container.querySelector("#bg_readable").onchange = liveBackground;
  const fileInput = container.querySelector("#bg_file");
  container.querySelector("#bg_choose").onclick = () => fileInput.click();
  fileInput.onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      await uploadBackground(file);
      cfg = await jget("/api/settings");
      applyBackground(cfg);
      toast("Background set.");
      render();
    } catch (err) { toast(err.message, true); }
  };
  container.querySelector("#bg_clear").onclick = async () => {
    try {
      await clearBackground();
      cfg = await jget("/api/settings");
      applyBackground(cfg);
      toast("Background cleared.");
      render();
    } catch (err) { toast(err.message, true); }
  };
}

async function save() {
  const body = {
    cover_art: container.querySelector('input[name="cover_art"]:checked').value,
    cover_art_embed_size: Math.max(0, parseInt(container.querySelector("#ca_size").value, 10) || 0),
    art_sources: {},
    theaudiodb_api_key: container.querySelector("#adb_key").value.trim(),
    discogs_token: container.querySelector("#dgs_token").value.trim(),
    background_opacity: parseFloat(container.querySelector("#bg_opacity").value),
    background_blur: parseInt(container.querySelector("#bg_blur").value, 10),
    background_fit: container.querySelector("#bg_fit").value,
    background_readable: container.querySelector("#bg_readable").checked,
    theme_accent_color: selectedAccent,
  };
  container.querySelectorAll("[data-bool]").forEach(c => body[c.dataset.bool] = c.checked);
  container.querySelectorAll("[data-src]").forEach(c => body.art_sources[c.dataset.src] = c.checked);
  try {
    cfg = await jpost("/api/settings", body);
    dirty = false;
    toast("Settings saved.");
  } catch (e) { toast(e.message, true); }
}

// Called by the router before switching away. Prompts on unsaved changes.
export async function beforeLeave() {
  if (!dirty) return true;
  const choice = await leavePrompt();
  if (choice === "cancel") return false;
  if (choice === "save") {
    await save();
  } else {   // revert: drop edits and undo live background/accent previews
    cfg = await jget("/api/settings");
    applyBackground(cfg);
    setAccentEnabled(cfg.theme_accent_from_art);
    setAccentBase(cfg.theme_accent_color);
  }
  dirty = false;
  return true;
}

function leavePrompt() {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; closeModal(); resolve(v); } };
    openModal(
      `<h3>Unsaved settings changes</h3>
       <p class="muted">You have unsaved changes. Save them before leaving?</p>
       <div class="row">
         <button class="btn" data-k="cancel">Cancel</button>
         <button class="btn danger" data-k="revert">Discard</button>
         <button class="btn primary" data-k="save">Save &amp; leave</button>
       </div>`,
      (box) => {
        box.querySelectorAll("[data-k]").forEach(b => b.onclick = () => finish(b.dataset.k));
        box.tabIndex = -1;
        box.focus();
        box.onkeydown = e => { if (e.key === "Escape") finish("cancel"); };
      });
  });
}
