// Entry point: top nav + view router.
import { jget, toast } from "./util.js";
import { getMode, setMode, onModeChange } from "./mode.js";
import { getTheme, toggleTheme } from "./theme.js";
import { initBackground } from "./background.js";
import { subscribeJob, cancelJob, jobLabel, initJobs } from "./jobs.js";
import { initPlayer } from "./player.js";
import { initSearch } from "./search.js";
import * as accent from "./accent.js";
import { escapeHtml } from "./util.js";
import * as browse from "./tree.js";
import * as audit from "./audit.js";
import * as standardize from "./standardize.js";
import * as importView from "./import.js";
import * as syncView from "./sync.js";
import * as settings from "./settings.js";

const VIEWS = [
  ["browse", "Browse", browse, "♪"],
  ["audit", "Audit", audit, "✓"],
  ["standardize", "Standardize", standardize, "✦"],
  ["import", "Import", importView, "↧"],
  ["sync", "Sync", syncView, "⇄"],
  ["settings", "Settings", settings, "⚙"],
];

const viewEl = document.getElementById("view");
const sidebar = document.getElementById("sidebar");
let current = null;
// Owners (loopback) get the full UI; network guests get read-only browse+play.
// Fails safe to guest if /api/whoami can't be reached.
let TRUSTED = false;

async function activate(name) {
  const entry = VIEWS.find(v => v[0] === name);
  if (!entry) return;
  // Let the outgoing view veto the switch (e.g. unsaved Settings changes).
  if (current && current !== name) {
    const curMod = (VIEWS.find(v => v[0] === current) || [])[2];
    if (curMod && curMod.beforeLeave) {
      const ok = await curMod.beforeLeave();
      if (!ok) return;
    }
  }
  current = name;
  for (const btn of sidebar.children) btn.classList.toggle("active", btn.dataset.name === name);
  // Mobile master-detail hooks live on #view: mark Browse, and always start on
  // the master (artist list) when (re)entering a view.
  viewEl.classList.toggle("browse", name === "browse");
  viewEl.classList.remove("show-detail");
  viewEl.innerHTML = "";
  try {
    entry[2].show(viewEl);
  } catch (e) {
    viewEl.innerHTML = `<div class="page"><p class="err">${e.message}</p></div>`;
  }
}

// Jump from the player to the currently playing album in Browse. The artist dir
// is the album path's parent; requestReveal must run before we (re)mount Browse.
function revealPlaying({ album_path, track_path }) {
  if (!album_path) return;
  const artist_path = album_path.slice(0, album_path.lastIndexOf("/"));
  browse.requestReveal({ artist_path, album_path, track_path });
  activate("browse");
}

function buildNav() {
  // Guests only get Browse; the other views are owner-only admin tools.
  const views = TRUSTED ? VIEWS : VIEWS.filter(v => v[0] === "browse");
  for (const [name, label, , icon] of views) {
    const btn = document.createElement("button");
    btn.innerHTML = `<span class="navicon">${icon}</span><span class="navlabel">${escapeHtml(label)}</span>`;
    btn.dataset.name = name;
    btn.onclick = () => activate(name);
    sidebar.appendChild(btn);
  }
}

function buildModeToggle() {
  const wrap = document.getElementById("modeToggle");
  // Edit mode is owner-only. Force browse and hide the toggle for guests so the
  // edit-only affordances in tree.js (inline fields, delete, drag-reorder) never
  // render — and even a stale localStorage "edit" can't resurrect them.
  if (!TRUSTED) { setMode("browse"); wrap.style.display = "none"; return; }
  const mk = (name, label) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.dataset.mode = name;
    b.onclick = () => setMode(name);
    return b;
  };
  wrap.append(mk("browse", "Browse"), mk("edit", "Edit"));
  const reflect = () => {
    for (const b of wrap.children) b.classList.toggle("active", b.dataset.mode === getMode());
  };
  reflect();
  onModeChange(reflect);
}

function buildThemeToggle() {
  const btn = document.getElementById("themeToggle");
  // Show the icon for the theme you'd switch TO.
  const reflect = () => { btn.textContent = getTheme() === "dark" ? "☀" : "☾"; };
  reflect();
  btn.onclick = () => { toggleTheme(); reflect(); accent.refresh(); };
}

function buildJobIndicator() {
  const wrap = document.getElementById("jobIndicator");
  subscribeJob(job => {
    const busy = job && (job.state === "running" || job.state === "waiting");
    if (!busy) { wrap.classList.remove("show"); wrap.innerHTML = ""; return; }
    wrap.classList.add("show");
    wrap.innerHTML = `<span class="dot"></span>
      <span><b>${escapeHtml(jobLabel(job.kind))}</b>
        <span class="muted">${escapeHtml(job.progress || "running…")}</span></span>
      <button class="iconbtn" data-cancel title="Cancel">✕</button>`;
    wrap.querySelector("[data-cancel]").onclick = () => cancelJob();
  });
}

async function init() {
  try {
    const who = await jget("/api/whoami");
    TRUSTED = !!who.trusted;
  } catch (e) {
    TRUSTED = false;   // fail safe to read-only
  }
  buildNav();
  buildModeToggle();
  buildThemeToggle();
  initBackground();
  if (TRUSTED) buildJobIndicator();   // jobs are owner-only operations
  initPlayer(revealPlaying);
  initSearch(activate);
  accent.initAccent();
  try {
    const data = await jget("/api/tree");
    document.getElementById("rootLabel").textContent = data.root;
  } catch (e) {
    toast(e.message, true);
  }
  activate("browse");
  if (TRUSTED) initJobs();   // resume tracking a job that was already running
  initForegroundWarmup();
}

// When the PWA returns to the foreground, the keep-alive socket Safari held may be
// dead. Fire a cheap request to detect that and open a fresh connection early, so
// the user's first tap doesn't pay the full fetch timeout+retry. Debounced so a
// flurry of visibility toggles only probes once.
function initForegroundWarmup() {
  let timer = 0;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    clearTimeout(timer);
    timer = setTimeout(() => { jget("/api/whoami").catch(() => {}); }, 150);
  });
}

init();
