// Entry point: top nav + view router.
import { jget, toast } from "./util.js";
import { getMode, setMode, onModeChange } from "./mode.js";
import * as browse from "./tree.js";
import * as audit from "./audit.js";
import * as standardize from "./standardize.js";
import * as importView from "./import.js";
import * as settings from "./settings.js";

const VIEWS = [
  ["browse", "Browse", browse],
  ["audit", "Audit", audit],
  ["standardize", "Standardize", standardize],
  ["import", "Import", importView],
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

async function init() {
  buildNav();
  buildModeToggle();
  try {
    const data = await jget("/api/tree");
    document.getElementById("rootLabel").textContent = data.root;
  } catch (e) {
    toast(e.message, true);
  }
  activate("browse");
}

init();
