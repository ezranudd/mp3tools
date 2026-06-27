// Entry point: top nav + view router.
import { jget, toast } from "./util.js";
import { getMode, setMode, onModeChange } from "./mode.js";
import { getTheme, toggleTheme } from "./theme.js";
import { subscribeJob, cancelJob, jobLabel, initJobs } from "./jobs.js";
import { initPlayer } from "./player.js";
import { escapeHtml } from "./util.js";
import * as browse from "./tree.js";
import * as audit from "./audit.js";
import * as standardize from "./standardize.js";
import * as importView from "./import.js";
import * as syncView from "./sync.js";
import * as settings from "./settings.js";

const VIEWS = [
  ["browse", "Browse", browse],
  ["audit", "Audit", audit],
  ["standardize", "Standardize", standardize],
  ["import", "Import", importView],
  ["sync", "Sync", syncView],
  ["settings", "Settings", settings],
];

const viewEl = document.getElementById("view");
const sidebar = document.getElementById("sidebar");
let current = null;

function activate(name) {
  const entry = VIEWS.find(v => v[0] === name);
  if (!entry) return;
  current = name;
  for (const btn of sidebar.children) btn.classList.toggle("active", btn.dataset.name === name);
  viewEl.innerHTML = "";
  try {
    entry[2].show(viewEl);
  } catch (e) {
    viewEl.innerHTML = `<div class="page"><p class="err">${e.message}</p></div>`;
  }
}

function buildNav() {
  for (const [name, label] of VIEWS) {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.dataset.name = name;
    btn.onclick = () => activate(name);
    sidebar.appendChild(btn);
  }
}

function buildModeToggle() {
  const wrap = document.getElementById("modeToggle");
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
  btn.onclick = () => { toggleTheme(); reflect(); };
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
  buildNav();
  buildModeToggle();
  buildThemeToggle();
  buildJobIndicator();
  initPlayer();
  try {
    const data = await jget("/api/tree");
    document.getElementById("rootLabel").textContent = data.root;
  } catch (e) {
    toast(e.message, true);
  }
  activate("browse");
  initJobs();   // resume tracking a job that was already running
}

init();
