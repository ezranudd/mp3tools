// Entry point: top nav + view router.
import { jget, jpost, toast, clientId, setAuthHandler, escapeAttr, openModal, getPref, setPref } from "./util.js";
import { getMode, setMode, onModeChange } from "./mode.js";
import { getTheme, toggleTheme, initSystemTheme } from "./theme.js";
import { initBackground } from "./background.js";
import { subscribeJob, cancelJob, jobLabel, initJobs } from "./jobs.js";
import { initPlayer } from "./player.js";
import { initSearch } from "./search.js";
import * as accent from "./accent.js";
import { escapeHtml } from "./util.js";
import * as browse from "./tree.js";
import { goBack } from "./tree.js";
import * as audit from "./audit.js";
import * as standardize from "./standardize.js";
import * as importView from "./import.js";
import * as ripView from "./rip.js";
import * as syncView from "./sync.js";
import * as settings from "./settings.js";
import * as devices from "./devices.js";
import * as access from "./access.js";

const VIEWS = [
  ["browse", "Browse", browse, "♪"],
  ["devices", "Devices", devices, "📡"],
  ["access", "Access", access, "🔐"],
  ["audit", "Audit", audit, "✓"],
  ["standardize", "Standardize", standardize, "✦"],
  ["import", "Import", importView, "↧"],
  ["rip", "Import CD", ripView, "💿"],
  ["sync", "Sync", syncView, "⇄"],
  ["settings", "Settings", settings, "⚙"],
];

// cid lets the server's Devices view tell this browser apart from others.
const WHOAMI_URL = "/api/whoami?cid=" + encodeURIComponent(clientId());

// Phones/tablets (touch, no hover): the mobile top bar + full-screen search live
// here; the theme follows the OS rather than a manual toggle.
const IS_MOBILE = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
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
  // the master (artist list) when (re)entering a view. The body mirror drives the
  // floating back FAB, so clear it here too.
  viewEl.classList.toggle("browse", name === "browse");
  viewEl.classList.remove("show-detail", "show-index");
  document.body.classList.remove("show-detail", "show-index");
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

let APP_STARTED = false;   // initApp() is idempotent — only the first call wires the UI

// Entry: resolve our role, then either gate (login/pending) or start the app.
async function boot() {
  let who;
  try {
    who = await jget(WHOAMI_URL);
  } catch (e) {
    who = { role: "lan" };   // unreachable whoami → fail safe to read-only
  }
  const role = who.role || (who.trusted ? "owner" : "lan");
  if (role === "anonymous") return showLogin(who);
  if (role === "pending")   return showPending();
  if (role === "blocked")   return showBlocked();
  hideAuthGate();
  TRUSTED = role === "owner";
  initApp();
}

function initApp() {
  if (APP_STARTED) return;   // already wired (e.g. boot() re-ran after login)
  APP_STARTED = true;
  document.body.classList.toggle("guest", !TRUSTED);   // drives mobile guest CSS
  buildNav();
  buildModeToggle();
  if (IS_MOBILE) initSystemTheme(accent.refresh);   // follow OS theme, no toggle
  else buildThemeToggle();
  initBackground();
  if (TRUSTED) buildJobIndicator();   // jobs are owner-only operations
  initPlayer(revealPlaying);
  initSearch(name => { closeSearch(); activate(name); });
  initMobileControls();
  accent.initAccent();
  jget("/api/tree")
    .then(data => { document.getElementById("rootLabel").textContent = data.root; })
    .catch(e => toast(e.message, true));
  activate("browse");
  if (TRUSTED) initJobs();   // resume tracking a job that was already running
  initForegroundWarmup();
}

// ── Remote-access gates (only ever shown in --remote mode) ────────────────────

function authGate() {
  let el = document.getElementById("authgate");
  if (!el) {
    el = document.createElement("div");
    el.id = "authgate";
    document.body.appendChild(el);
  }
  el.style.display = "flex";
  return el;
}
function hideAuthGate() {
  const el = document.getElementById("authgate");
  if (el) el.style.display = "none";
}

function showLogin(who) {
  const warn = who && who.password_set === false
    ? `<p class="err">No access password is set on the server yet.</p>` : "";
  const el = authGate();
  el.innerHTML = `<div class="authbox card">
    <h2>Sign in</h2>
    <p class="muted">This music library is private. Enter the access password.</p>
    ${warn}
    <input id="authpw" type="password" autocomplete="current-password" placeholder="Access password" style="width:100%">
    <input id="authname" type="text" placeholder="Device name (optional, e.g. “Ezra’s phone”)" style="width:100%;margin-top:8px">
    <div id="autherr" class="err" style="min-height:1.2em;margin:6px 0"></div>
    <button id="authbtn" class="btn primary" style="width:100%">Sign in</button>
  </div>`;
  const pw = el.querySelector("#authpw");
  const nm = el.querySelector("#authname");
  const err = el.querySelector("#autherr");
  const btn = el.querySelector("#authbtn");
  pw.focus();
  const submit = async () => {
    err.textContent = "";
    btn.disabled = true;
    try {
      const res = await jpost("/api/auth/login", {
        password: pw.value, cid: clientId(), device_name: nm.value.trim(),
      });
      if (res.role === "pending") return showPending();
      hideAuthGate();
      await boot();   // member → start the app
    } catch (e) {
      err.textContent = e.message || "Sign-in failed";
      btn.disabled = false;
      pw.select();
    }
  };
  btn.onclick = submit;
  pw.onkeydown = e => { if (e.key === "Enter") submit(); };
  nm.onkeydown = e => { if (e.key === "Enter") submit(); };
}

let _pendingTimer = 0;
function showPending() {
  const el = authGate();
  el.innerHTML = `<div class="authbox card">
    <h2>Waiting for approval</h2>
    <p class="muted">Your device signed in, but the owner needs to approve it
      before you can browse. This page will update automatically once approved.</p>
    <div class="spinner" style="margin:14px 0">…</div>
    <button id="authlogout" class="btn" style="width:100%">Cancel</button>
  </div>`;
  el.querySelector("#authlogout").onclick = async () => {
    clearInterval(_pendingTimer); _pendingTimer = 0;
    try { await jpost("/api/auth/logout", {}); } catch {}
    showLogin({});
  };
  clearInterval(_pendingTimer);
  _pendingTimer = setInterval(async () => {
    let who;
    try { who = await jget(WHOAMI_URL); } catch { return; }
    if (who.role === "member" || who.role === "owner") {
      clearInterval(_pendingTimer); _pendingTimer = 0;
      hideAuthGate();
      TRUSTED = who.role === "owner";
      initApp();
    } else if (who.role === "blocked") {
      clearInterval(_pendingTimer); _pendingTimer = 0;
      showBlocked();
    }
  }, 4000);
}

function showBlocked() {
  const el = authGate();
  el.innerHTML = `<div class="authbox card">
    <h2>Access blocked</h2>
    <p class="muted">This device has been blocked by the owner.</p>
    <button id="authlogout" class="btn" style="width:100%">Sign out</button>
  </div>`;
  el.querySelector("#authlogout").onclick = async () => {
    try { await jpost("/api/auth/logout", {}); } catch {}
    showLogin({});
  };
}

// Mobile floating controls: a search toggle that opens the search field overlay,
// and a back FAB that returns from an album/artist page to the artist list. Both
// are inert on desktop (their CSS hides them); wiring them unconditionally is fine.
function openSearch() {
  document.body.classList.add("search-open");
  const inp = document.getElementById("searchInput");
  if (inp) { inp.focus(); inp.select(); }
}
function closeSearch() { document.body.classList.remove("search-open"); }

// Per-device playback prefs (the gapless-streaming opt-in). Lives in a modal off
// the mobile header gear rather than the Settings view, because Settings is
// owner-only — but the people who need this toggle are mobile guests on the LAN.
// The pref is read at playAlbum time, so a change applies to the next album.
function openPlaybackPrefs() {
  const on = getPref("gapless_stream") === "1";
  openModal(
    `<h3>Playback (this device)</h3>
     <label class="field" style="display:flex;align-items:center;gap:10px;cursor:pointer">
       <input type="checkbox" id="pp_gapless" ${on ? "checked" : ""}>
       <span>Gapless album streaming</span>
     </label>
     <p class="muted">Play a whole album as one continuous stream so tracks run
       together with no gap. Uses more data, and seeking / track-skip is a little
       less snappy. Applies from the next album you play. Saved on this device only.</p>
     <div class="row"><button class="btn primary" data-close>Done</button></div>`,
    (box, close) => {
      box.querySelector("#pp_gapless").onchange =
        (e) => setPref("gapless_stream", e.target.checked ? "1" : "0");
      box.querySelector("[data-close]").onclick = close;
    });
}

function initMobileControls() {
  // Relocate the search box into the full-screen overlay on phones so it escapes
  // the header's backdrop-filter containing block (which otherwise traps the
  // results panel to a tiny box). search.js binds by id, so this is transparent.
  if (IS_MOBILE) {
    const overlay = document.getElementById("searchOverlay");
    const search = document.getElementById("search");
    if (overlay && search) overlay.appendChild(search);
  }

  const toggle = document.getElementById("searchToggle");
  if (toggle) toggle.onclick = () =>
    document.body.classList.contains("search-open") ? closeSearch() : openSearch();

  const close = document.getElementById("searchClose");
  if (close) close.onclick = closeSearch;

  const prefs = document.getElementById("prefsToggle");
  if (prefs) prefs.onclick = openPlaybackPrefs;

  const back = document.getElementById("backFab");
  if (back) back.onclick = goBack;

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && document.body.classList.contains("search-open")) closeSearch();
  });
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
    timer = setTimeout(() => { jget(WHOAMI_URL).catch(() => {}); }, 150);
  });
}

// A 401 from anywhere (remote session expired) drops back to the login gate,
// unless we're already showing it.
setAuthHandler(() => {
  const el = document.getElementById("authgate");
  if (el && el.style.display === "flex") return;
  showLogin({});
});

boot();
