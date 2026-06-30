// Browse view: library tree (left) + detail (right).
// Selecting an album shows that album; selecting an artist shows ALL its albums.
// Read-only in Browse mode; inline auto-saving fields in Edit mode.
import { jget, jpost, toast, escapeHtml, escapeAttr, enableRowDrag, fmtDurationLong } from "./util.js";
import { isEdit, onModeChange } from "./mode.js";
import { isBusy, subscribeJob } from "./jobs.js";
import { playAlbum, subscribe as subscribePlayer, getCurrentPath } from "./player.js";
import * as edit from "./edit.js";

let CURRENT = null;   // selected artist: { kind: "artist", path }
let TREE = [];        // artist nodes from /api/tree
let GENRES = [];      // [{genre, count}] from /api/genres
let rootEl;           // the #view container (holds #browseSelect + #tree + #detail)
let treeEl, indexEl, detailEl;
let subscribed = false;
let pendingReveal = null;   // { artist_path, album_path, track_path? } from search
let browseMode = "artists"; // artists | genres — which index the left pane shows
let browseLevel = "select"; // select | index | detail (mobile drill-down level)
let genreAlbums = [];       // albums in the current genre grid
let genreName = "";         // current genre being shown
let genreSort = "az";       // az | date | rand

// Ask Browse to jump to an album/track once it's (re)mounted.
export function requestReveal(target) { pendingReveal = target; }

// Mobile drill-down: Browse has three levels — select (choose Artists/Genres),
// index (the chosen list), detail (album list / genre grid). On desktop the levels
// are visual no-ops (tabs + index + detail all show); the classes only drive the
// mobile CSS and the floating back FAB (which lives outside #view, so it keys off
// the body mirror). setLevel reflects the level as show-index / show-detail classes.
function setLevel(level) {
  browseLevel = level;
  const idx = level === "index" || level === "detail";
  const det = level === "detail";
  for (const el of [rootEl, document.body]) {
    if (!el) continue;
    el.classList.toggle("show-index", idx);
    el.classList.toggle("show-detail", det);
  }
}
function enterDetail() { setLevel("detail"); }
// Back goes up exactly one level: detail → index → select.
export function goBack() { setLevel(browseLevel === "detail" ? "index" : "select"); }
const BACK_BAR = `<div class="backbar"><button class="btn" data-back>‹ Back</button></div>`;
function wireBack() {
  const b = detailEl.querySelector("[data-back]");
  if (b) b.onclick = goBack;
}

export async function show(container) {
  container.innerHTML = `
    <div id="browseSelect">
      <button class="bigchoice" data-mode="artists"><span class="bcicon">♪</span><span>Artists</span></button>
      <button class="bigchoice" data-mode="genres"><span class="bcicon">🎵</span><span>Genres</span></button>
    </div>
    <nav id="tree">
      <div class="browsetabs">
        <button data-mode="artists">Artists</button>
        <button data-mode="genres">Genres</button>
      </div>
      <div id="indexList"></div>
    </nav>
    <section id="detail"><p class="muted">Select an artist or album.</p></section>`;
  rootEl = container;
  treeEl = container.querySelector("#tree");
  indexEl = container.querySelector("#indexList");
  detailEl = container.querySelector("#detail");
  // Both the big landing buttons (mobile) and the compact tabs (desktop) switch mode.
  container.querySelectorAll("[data-mode]").forEach(b =>
    b.onclick = () => setBrowseMode(b.dataset.mode));
  if (!subscribed) {
    subscribed = true;
    // Re-render when the mode flips, or when a job starts/ends (edits get
    // blocked while an operation runs). Only matters while Browse is mounted.
    onModeChange(rerender);
    let lastBusy = isBusy();
    subscribeJob(() => {
      if (isBusy() !== lastBusy) { lastBusy = isBusy(); rerender(); }
    });
    subscribePlayer(updatePlayingHighlight);
  }
  // Start at the select level (mobile shows the two options; desktop shows
  // everything side by side), with the default mode's index pre-loaded.
  setLevel("select");
  if (pendingReveal) {
    const target = pendingReveal;
    pendingReveal = null;
    browseMode = "artists";
    await loadMode("artists");
    applyReveal(target);
  } else {
    await loadMode(browseMode);
  }
}

// Switch the active index between Artists and Genres and reflect it on the tabs.
// Pure load — does not change the drill-down level.
function loadMode(mode) {
  browseMode = mode;
  if (rootEl) rootEl.querySelectorAll("[data-mode]").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === mode));
  return mode === "genres" ? loadGenres() : loadTree();
}

// User picked a mode (tab or landing button): load it and drill into the index.
function setBrowseMode(mode) {
  detailEl.innerHTML = `<p class="muted">Select ${mode === "genres" ? "a genre" : "an artist"}.</p>`;
  setLevel("index");
  loadMode(mode);
}

// The artist tree backs selectArtist(); make sure it's loaded even when the user
// reached here genres-first (so the genre grid was the only thing fetched).
async function ensureTree() {
  if (TREE.length) return;
  try { TREE = (await jget("/api/tree")).artists; }
  catch (e) { toast(e.message, true); }
}

async function applyReveal({ artist_path, album_path, track_path }) {
  await ensureTree();
  await selectArtist(artist_path);
  if (!isCurrent("artist", artist_path)) return;
  const sec = album_path
    ? detailEl.querySelector(`.albumsection[data-path="${CSS.escape(album_path)}"]`) : null;
  if (track_path) {
    const tr = detailEl.querySelector(`tr[data-path="${CSS.escape(track_path)}"]`);
    if (tr) {
      tr.scrollIntoView({ block: "center" });
      tr.classList.add("flash");
      setTimeout(() => tr.classList.remove("flash"), 1500);
      return;
    }
  }
  if (sec) sec.scrollIntoView({ block: "start" });
}

// Mark the row(s) whose data-path matches the currently playing track.
function updatePlayingHighlight(path) {
  if (!detailEl) return;
  detailEl.querySelectorAll("tr[data-path]").forEach(tr =>
    tr.classList.toggle("playing", tr.dataset.path === path));
}

function rerender() {
  if (!treeEl || !treeEl.isConnected) return;
  const anchor = captureAnchor();
  loadMode(browseMode).then(() => {
    if (CURRENT) selectArtist(CURRENT.path).then(() => restoreAnchor(anchor));
  });
}

// Remember which album section sits at the top of #detail (and its sub-offset),
// so a re-render that changes section heights (Browse↔Edit) can restore the view.
function captureAnchor() {
  if (!detailEl) return null;
  const cTop = detailEl.getBoundingClientRect().top;
  for (const sec of detailEl.querySelectorAll(".albumsection")) {
    const r = sec.getBoundingClientRect();
    if (r.bottom > cTop + 1) return { path: sec.dataset.path, delta: r.top - cTop };
  }
  return { scrollTop: detailEl.scrollTop };   // fallback: no album sections
}

function restoreAnchor(a) {
  if (!a || !detailEl) return;
  if (a.path) {
    const sec = detailEl.querySelector(`.albumsection[data-path="${CSS.escape(a.path)}"]`);
    if (sec) {
      const cTop = detailEl.getBoundingClientRect().top;
      detailEl.scrollTop += (sec.getBoundingClientRect().top - cTop) - a.delta;
      return;
    }
  }
  if (a.scrollTop != null) detailEl.scrollTop = a.scrollTop;
}

async function loadTree() {
  indexEl.innerHTML = `<p class="muted" style="padding:10px">Loading…</p>`;
  try {
    const data = await jget("/api/tree");
    TREE = data.artists;
    indexEl.innerHTML = "";
    for (const artist of TREE) indexEl.appendChild(artistEl(artist));
    if (!TREE.length) indexEl.innerHTML = `<p class="muted" style="padding:10px">Empty library.</p>`;
  } catch (e) {
    indexEl.innerHTML = `<p class="err" style="padding:10px">${escapeHtml(e.message)}</p>`;
  }
}

async function loadGenres() {
  indexEl.innerHTML = `<p class="muted" style="padding:10px">Loading…</p>`;
  try {
    const data = await jget("/api/genres");
    GENRES = data.genres || [];
    indexEl.innerHTML = "";
    for (const g of GENRES) indexEl.appendChild(genreNodeEl(g));
    if (!GENRES.length) indexEl.innerHTML = `<p class="muted" style="padding:10px">No genres.</p>`;
  } catch (e) {
    indexEl.innerHTML = `<p class="err" style="padding:10px">${escapeHtml(e.message)}</p>`;
  }
}

// A genre row in the index: name + album-count badge. In Edit mode (owner) a merge
// glyph re-tags every album of this genre into another.
function genreNodeEl(g) {
  const head = document.createElement("div");
  head.className = "node genre";
  head.dataset.genre = g.genre;
  head.innerHTML =
    (isEdit() && !isBusy() ? `<span class="nodeact" title="Merge genre">⧉</span>` : "") +
    `<span class="gname">${escapeHtml(g.genre)}</span><span class="gcount">${g.count}</span>`;
  head.onclick = (e) => {
    if (e.target.classList.contains("nodeact")) {
      edit.mergeGenre(g.genre, GENRES, () => loadGenres());
      return;
    }
    showGenre(g.genre, head);
  };
  return head;
}

// ── Tree nodes (flat artist list — click an artist to see all its albums) ─────

function clearSel() {
  treeEl.querySelectorAll(".node.sel").forEach(n => n.classList.remove("sel"));
}
function artistNodeEl(path) {
  return path ? treeEl.querySelector(`.node.artist[data-path="${CSS.escape(path)}"]`) : null;
}

function artistEl(artist) {
  const head = document.createElement("div");
  head.className = "node artist";
  head.dataset.path = artist.path;
  head.innerHTML =
    (isEdit() && !isBusy() ? `<span class="nodeact" title="Edit artist">✎</span>` : "") +
    escapeHtml(artist.label);
  head.onclick = (e) => {
    if (e.target.classList.contains("nodeact")) { edit.editArtist(artist, loadTree); return; }
    selectArtist(artist.path, head);
  };
  return head;
}

// ── Selection ─────────────────────────────────────────────────────────────────

async function fetchAlbumState(path) {
  try {
    const data = await jget("/api/album?path=" + encodeURIComponent(path));
    const first = data.tracks[0] || {};
    return {
      path,
      tracks: data.tracks,
      artist: first.albumartist || first.artist || "",
      album: first.album || "",
      year: first.year || "",
      genre: first.genre || "",
    };
  } catch (e) { toast(e.message, true); return null; }
}

async function selectArtist(path, headEl) {
  CURRENT = { kind: "artist", path };
  clearSel();
  enterDetail();
  headEl = headEl || artistNodeEl(path);
  if (headEl) headEl.classList.add("sel");
  const artist = TREE.find(a => a.path === path);
  if (!artist) { detailEl.innerHTML = `${BACK_BAR}<p class="muted">Artist not found.</p>`; wireBack(); return; }
  detailEl.innerHTML = `
    ${BACK_BAR}
    ${editPausedNotice()}
    <div class="artisthead">
      <h2>${escapeHtml(artist.label)}</h2>
      <div class="sub">${artist.children.length} album${artist.children.length === 1 ? "" : "s"}</div>
    </div>
    <div id="artistAlbums"></div>`;
  wireBack();
  const host = detailEl.querySelector("#artistAlbums");
  if (!artist.children.length) { host.innerHTML = `<p class="muted">No albums.</p>`; return; }

  const states = await Promise.all(artist.children.map(a => fetchAlbumState(a.path)));
  if (!isCurrent("artist", path)) return;     // a newer selection won the race
  host.innerHTML = "";
  for (const st of states) {
    if (!st) continue;
    const sec = document.createElement("section");
    sec.className = "albumsection";
    sec.dataset.path = st.path;
    host.appendChild(sec);
    renderAlbumInto(sec, st);
  }
}

function isCurrent(kind, path) {
  return CURRENT && CURRENT.kind === kind && CURRENT.path === path;
}

async function refreshCurrent() {
  if (CURRENT) selectArtist(CURRENT.path);
}

// ── Genre grid (click an album's genre to see all same-genre albums) ──────────

async function showGenre(genre, headEl) {
  CURRENT = null;     // a grid, not an artist — leave it alone on job/mode rerenders
  clearSel();
  if (headEl) headEl.classList.add("sel");
  enterDetail();
  genreName = genre;
  genreSort = "az";
  detailEl.innerHTML = `<p class="muted">Loading…</p>`;
  let data;
  try { data = await jget("/api/genre?name=" + encodeURIComponent(genre)); }
  catch (e) { toast(e.message, true); return; }
  genreAlbums = data.albums || [];
  detailEl.innerHTML = `
    ${BACK_BAR}
    <div class="artisthead genrehead">
      <h2>Genre · ${escapeHtml(genre)}</h2>
      <div class="sub">${genreAlbums.length} album${genreAlbums.length === 1 ? "" : "s"}</div>
      <div class="sortbar">
        <span class="muted">Sort:</span>
        <button class="btn sortbtn" data-sort="az">A–Z</button>
        <button class="btn sortbtn" data-sort="date">Date</button>
        <button class="btn sortbtn" data-sort="rand">Random</button>
      </div>
    </div>
    <div class="genregrid" id="genreGrid"></div>`;
  wireBack();
  detailEl.querySelectorAll(".sortbtn").forEach(b =>
    b.onclick = () => { genreSort = b.dataset.sort; renderGenreGrid(); });
  renderGenreGrid();
}

function sortedGenreAlbums() {
  const list = genreAlbums.slice();
  if (genreSort === "az") {
    list.sort((a, b) => (a.album || "").localeCompare(b.album || "", undefined, { sensitivity: "base" }));
  } else if (genreSort === "date") {
    list.sort((a, b) => (a.year || "9999").localeCompare(b.year || "9999"));
  } else {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
  }
  return list;
}

function renderGenreGrid() {
  const grid = detailEl.querySelector("#genreGrid");
  if (!grid) return;
  detailEl.querySelectorAll(".sortbtn").forEach(b =>
    b.classList.toggle("active", b.dataset.sort === genreSort));
  if (!genreAlbums.length) {
    grid.innerHTML = `<p class="muted">No albums in this genre.</p>`;
    return;
  }
  grid.innerHTML = sortedGenreAlbums().map(a => {
    const cover = "/api/cover?path=" + encodeURIComponent(a.album_path);
    return `<div class="gcard" data-album="${escapeAttr(a.album_path)}"
                 data-artist="${escapeAttr(a.artist_path)}" title="${escapeAttr((a.album || "") + " — " + (a.artist || ""))}">
        <img src="${cover}" loading="lazy" onerror="this.style.visibility='hidden'">
        <div class="gcap"><b>${escapeHtml(a.album || "")}</b><span>${escapeHtml(a.artist || "")}</span></div>
      </div>`;
  }).join("");
  grid.querySelectorAll(".gcard").forEach(card =>
    card.onclick = () => applyReveal({
      artist_path: card.dataset.artist,
      album_path: card.dataset.album,
      track_path: null,
    }));
}

// ── Album rendering (into an arbitrary container, bound to a state object) ─────

function renderAlbumInto(container, st) {
  // Edits are paused while an operation runs — fall back to read-only.
  if (isEdit() && !isBusy()) return renderAlbumEditInto(container, st);
  return renderAlbumBrowseInto(container, st);
}

function editPausedNotice() {
  return (isEdit() && isBusy())
    ? `<div class="notice">Editing is paused while an operation is running.</div>` : "";
}

function albumHead(st, innerMeta) {
  const cover = "/api/cover?path=" + encodeURIComponent(st.path) + "&t=" + Date.now();
  return `<div class="albumhead">
      <img class="cover" src="${cover}" onerror="this.style.visibility='hidden'">
      <div class="albummeta">${innerMeta}</div>
    </div>`;
}

function renderAlbumBrowseInto(container, st) {
  const { tracks, artist, album, year, genre } = st;
  const subParts = [escapeHtml(artist || "(unknown artist)")];
  if (year) subParts.push(escapeHtml(year));
  if (genre) subParts.push(`<span class="genrelink" data-genre="${escapeAttr(genre)}">${escapeHtml(genre)}</span>`);
  const sub = subParts.join(" · ");
  const totalSec = tracks.reduce((a, t) => a + (Number(t.length_sec) || 0), 0);
  const countLabel = `${tracks.length} track${tracks.length === 1 ? "" : "s"}`;
  const metaLine = totalSec > 0 ? `${countLabel} · ${fmtDurationLong(totalSec)}` : countLabel;
  const rows = tracks.map(t => `
    <tr class="browserow" data-path="${escapeAttr(t.path)}">
      <td><span class="rowplay">▶</span> <span class="num">${escapeHtml((t.track || "").split("/")[0])}</span></td>
      <td>${escapeHtml(t.title || "")}</td>
      <td>${escapeHtml(t.artist || "")}</td>
      <td class="tdur muted">${escapeHtml(t.length || "")}</td>
      <td class="muted">${escapeHtml(t.bitrate ? t.bitrate + " kbps" : "")}</td>
    </tr>`).join("");
  container.innerHTML = albumHead(st, `
      <h2>${escapeHtml(album || "(untitled)")}</h2>
      <div class="sub">${sub}</div>
      <div class="sub">${metaLine}</div>`) + `
    <table class="browsetable">
      <thead><tr><th>#</th><th>Title</th><th>Artist</th><th class="tdur">Time</th><th>Rate</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  container.querySelectorAll("tr.browserow").forEach((tr, i) =>
    tr.onclick = () => playAlbum(tracks, i, st.path));
  container.querySelectorAll(".genrelink").forEach(el =>
    el.onclick = (e) => { e.stopPropagation(); showGenre(el.dataset.genre); });
  updatePlayingHighlight(getCurrentPath());
}

function renderAlbumEditInto(container, st) {
  const { tracks, artist, album, year, genre } = st;
  const totalSec = tracks.reduce((a, t) => a + (Number(t.length_sec) || 0), 0);
  const countLabel = `${tracks.length} track${tracks.length === 1 ? "" : "s"}`;
  const metaLine = totalSec > 0 ? `${countLabel} · ${fmtDurationLong(totalSec)}` : countLabel;
  const rows = tracks.map(t => `
    <tr data-path="${escapeAttr(t.path)}">
      <td><span class="draghandle" title="Drag to reorder">⠿</span> <span class="num">${escapeHtml((t.track || "").split("/")[0])}</span></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TIT2"
                 value="${escapeAttr(t.title || "")}"></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TPE1"
                 value="${escapeAttr(t.artist || "")}"></td>
      <td class="tdur muted">${escapeHtml(t.length || "")}</td>
      <td class="muted">${escapeHtml(t.bitrate ? t.bitrate + " kbps" : "")}</td>
    </tr>`).join("");
  container.innerHTML = albumHead(st, `
      <input class="hdr title" data-op="album_title" value="${escapeAttr(album)}" placeholder="Album title">
      <div class="sub albumsub">
        <input class="hdr sub" data-op="album_artist" value="${escapeAttr(artist)}" placeholder="Album artist"> ·
        <input class="hdr sub" data-op="album_year" value="${escapeAttr(year)}" placeholder="Year"> ·
        <input class="hdr sub" data-op="album_genre" value="${escapeAttr(genre)}" placeholder="Genre">
      </div>
      <div class="sub">${metaLine}</div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn danger" data-act="del">Delete album</button>
      </div>`) + `
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Artist</th><th class="tdur">Time</th><th>Rate</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  // Track tag inputs — auto-save on commit (frame-only write).
  container.querySelectorAll("input.tag").forEach(inp => {
    inp._orig = inp.value;
    inp.oninput = () => inp.classList.toggle("dirty", inp.value !== inp._orig);
    bindCommit(inp, () => commitTrackField(inp.dataset.path, inp.dataset.frame, inp.value, inp));
  });

  // Album header inputs — auto-save on commit (structural edit).
  const orig = { album_title: album, album_artist: artist, album_year: year, album_genre: genre };
  container.querySelectorAll("input.hdr[data-op]").forEach(inp => {
    const op = inp.dataset.op;
    bindCommit(inp, () => commitAlbumField(st, op, inp.value, orig[op]));
  });

  // Size the sub-line fields to their content so the " · " separators stay tight.
  container.querySelectorAll(".albumsub input.hdr").forEach(inp => {
    autosizeField(inp);
    inp.addEventListener("input", () => autosizeField(inp));
  });

  // No playback in Edit mode (reorder by dragging instead); still reflect any
  // track already playing from Browse.
  updatePlayingHighlight(getCurrentPath());

  // Drag tracks to reorder → renumber/rename the files on disk, then refresh.
  const tbody = container.querySelector("tbody");
  if (tbody) enableRowDrag(tbody, async () => {
    const order = [...tbody.querySelectorAll("tr[data-path]")].map(tr => tr.dataset.path);
    try {
      const res = await jpost("/api/album/reorder", { path: st.path, order });
      if (!res.ok) toast(res.error || "Reorder failed", true);
    } catch (e) { toast(e.message, true); }
    refreshCurrent();
  });

  // Clicking the cover manages all artwork (search/apply, upload, remove).
  const coverImg = container.querySelector(".albumhead img.cover");
  if (coverImg) {
    coverImg.classList.add("editcover");
    coverImg.title = "Click to change cover art";
    coverImg.onclick = () => findArt(st);
  }

  container.querySelector('[data-act="del"]').onclick = () =>
    edit.deleteAlbum(st.path, st.album || st.path.split("/").pop(), afterAlbumDelete);
}

// After deleting an album, reload the tree and re-select the artist — unless that
// was its last album (the artist folder gets pruned, so fall back to the placeholder).
async function afterAlbumDelete() {
  const artistPath = CURRENT && CURRENT.path;
  await loadTree();
  if (artistPath && TREE.find(a => a.path === artistPath)) selectArtist(artistPath);
  else { CURRENT = null; detailEl.innerHTML = `<p class="muted">Select an artist or album.</p>`; }
}

// Commit on blur or Enter (Enter blurs to fire the change once).
function bindCommit(inp, fn) {
  inp.addEventListener("change", fn);
  inp.addEventListener("keydown", e => { if (e.key === "Enter") inp.blur(); });
}

// Width an input to fit its value (or placeholder) using a hidden measuring span,
// so inline header fields hug their content like the read-only text does.
let _measureEl = null;
function autosizeField(inp) {
  if (!_measureEl) {
    _measureEl = document.createElement("span");
    _measureEl.style.cssText =
      "position:absolute;left:-9999px;top:-9999px;visibility:hidden;white-space:pre;";
    document.body.appendChild(_measureEl);
  }
  const cs = getComputedStyle(inp);
  _measureEl.style.fontSize = cs.fontSize;
  _measureEl.style.fontFamily = cs.fontFamily;
  _measureEl.style.fontWeight = cs.fontWeight;
  _measureEl.style.fontStyle = cs.fontStyle;
  _measureEl.style.letterSpacing = cs.letterSpacing;
  _measureEl.textContent = inp.value || inp.placeholder || "";
  inp.style.width = (_measureEl.offsetWidth + 1) + "px";   // +1 for the caret
}

async function commitTrackField(path, frame, value, inp) {
  if (value === inp._orig) return;
  try {
    await jpost("/api/tags", { path, updates: { [frame]: value } });
    inp._orig = value;
    inp.classList.remove("dirty");
    toast("Saved.");
  } catch (e) { toast(e.message, true); }
}

async function commitAlbumField(st, op, value, current) {
  value = value.trim();
  if (value === (current || "") || !value) return;
  try {
    const res = await jpost("/api/edit/apply", { path: st.path, op, value });
    if (!res.ok || res.error) { toast(res.error || "Edit failed", true); return; }
    toast(res.desc || "Saved.");
    await loadTree();
    selectArtist(CURRENT.path);
  } catch (e) { toast(e.message, true); }
}

// ── Artwork search/apply (uses the shared modal) ──────────────────────────────

async function findArt(st) {
  const { artist, album } = st;
  const { openModal, closeModal } = await import("./util.js");
  openModal(`<h3>Artwork — ${escapeHtml(artist)} / ${escapeHtml(album)}</h3>
    <div id="artBody" class="grid"><p class="muted">Searching…</p></div>
    <div class="row">
      <input type="file" accept="image/*" id="artFile" style="display:none">
      <button class="btn" data-file>Choose local file…</button>
      <button class="btn danger" data-remove>Remove art</button>
      <button class="btn" data-close>Close</button>
    </div>`,
    (box) => {
      box.querySelector("[data-close]").onclick = closeModal;
      const file = box.querySelector("#artFile");
      box.querySelector("[data-file]").onclick = () => file.click();
      file.onchange = () => { if (file.files[0]) uploadArt(st, file.files[0], closeModal); };
      box.querySelector("[data-remove]").onclick = () => { closeModal(); edit.removeArt(st.path, refreshCurrent); };
    });
  try {
    const data = await jget(`/api/art/search?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`);
    const results = data.results || [];
    const body = document.getElementById("artBody");
    if (!body) return;
    if (!results.length) { body.innerHTML = `<p class="muted">No results.</p>`; return; }
    body.innerHTML = "";
    for (const r of results) {
      const card = document.createElement("div");
      card.className = "art";
      card.innerHTML = `<img src="${escapeAttr(r.url)}" loading="lazy">
        <div class="cap">${escapeHtml(r.source_label || r.source || "")}${r.size ? " · " + escapeHtml(r.size) : ""} · ${r.score ?? ""}</div>`;
      card.onclick = () => applyArt(st, r.url, closeModal);
      body.appendChild(card);
    }
  } catch (e) {
    const body = document.getElementById("artBody");
    if (body) body.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
  }
}

async function applyArt(st, url, close) {
  try {
    const res = await jpost("/api/art/apply", { path: st.path, url });
    close();
    toast(`Artwork applied (${res.updated} file${res.updated === 1 ? "" : "s"}).`);
    refreshCurrent();
  } catch (e) { toast(e.message, true); }
}

async function uploadArt(st, fileObj, close) {
  try {
    const r = await fetch("/api/art/upload?path=" + encodeURIComponent(st.path), {
      method: "POST",
      headers: { "Content-Type": fileObj.type || "image/jpeg" },
      body: fileObj,
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const res = await r.json();
    close();
    toast(`Artwork applied (${res.updated} file${res.updated === 1 ? "" : "s"}).`);
    refreshCurrent();
  } catch (e) { toast(e.message, true); }
}
