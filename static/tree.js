// Browse view: library tree (left) + detail (right).
// Selecting an album shows that album; selecting an artist shows ALL its albums.
// Read-only in Browse mode; inline auto-saving fields in Edit mode.
import { jget, jpost, toast, escapeHtml, escapeAttr, enableRowDrag, fmtDurationLong,
         setPlaceholder, openModal, closeModal } from "./util.js";
import { isEdit, onModeChange } from "./mode.js";
import { isBusy, subscribeJob } from "./jobs.js";
import { playAlbum, prewarmStream, subscribe as subscribePlayer, getCurrentPath } from "./player.js";
import * as edit from "./edit.js";

let CURRENT = null;   // selected artist: { kind: "artist", path }
let TREE = [];        // artist nodes from /api/tree
let GENRES = [];      // [{genre, count}] from /api/genres
let COLLECTIONS = []; // [{name, count}] from /api/collections
let rootEl;           // the #view container (holds #browseSelect + #tree + #detail)
let treeEl, indexEl, detailEl;
let subscribed = false;
let pendingReveal = null;   // { artist_path, album_path, track_path? } from search
let browseMode = "artists"; // artists | genres | albums | collections — which the left pane shows
let browseLevel = "select"; // select | index | detail (mobile drill-down level)
// Shared album grid (used by both the Genres mode and the Albums mode).
let gridAlbums = [];        // albums in the current grid
let gridKind = "";          // "genre" | "albums" — what the grid is showing
let gridName = "";          // the genre name when gridKind === "genre"
let gridSort = "az";        // az | date | rand
let gridEmptyMsg = "No albums.";
let albumsDrilled = false;  // drilled into an artist from the Albums grid (Back → grid)

// Ask Browse to jump to an album/track once it's (re)mounted.
export function requestReveal(target) { pendingReveal = target; }

// Mobile drill-down: Browse has three levels — select (choose Artists/Genres/Albums),
// index (the chosen list), detail (album list / grid). On desktop the levels
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
// Back goes up exactly one level. Albums has no index list, so its grid sits at the
// detail level: drilling into an artist from it returns to the grid, then to select.
export function goBack() {
  if (browseMode === "albums") {
    if (albumsDrilled) { albumsDrilled = false; reshowGrid(); }
    else setLevel("select");
    return;
  }
  setLevel(browseLevel === "detail" ? "index" : "select");
}
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
      <button class="bigchoice" data-mode="albums"><span class="bcicon">💿</span><span>Albums</span></button>
      <button class="bigchoice" data-mode="collections"><span class="bcicon">★</span><span>Collections</span></button>
    </div>
    <nav id="tree">
      <div class="browsetabs">
        <button data-mode="artists">Artists</button>
        <button data-mode="genres">Genres</button>
        <button data-mode="albums">Albums</button>
        <button data-mode="collections">Collections</button>
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
  // Start at the select level (mobile shows the mode options; desktop shows
  // everything side by side), with the active mode pre-loaded. loadMode("albums")
  // drills to its grid, so re-assert the select level afterward.
  setLevel("select");
  if (pendingReveal) {
    const target = pendingReveal;
    pendingReveal = null;
    browseMode = "artists";
    await loadMode("artists");
    applyReveal(target);
  } else {
    await loadMode(browseMode);
    setLevel("select");
  }
}

// Load the active mode and reflect it on the tabs. Pure load — does not change the
// drill-down level. Artists/Genres populate the left index list; Albums has no list
// (the grid lives in the wide detail pane) so it renders the grid directly.
function loadMode(mode) {
  browseMode = mode;
  if (rootEl) rootEl.querySelectorAll("[data-mode]").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === mode));
  if (mode === "albums") { indexEl.innerHTML = ""; return showAlbums(); }
  if (mode === "genres") return loadGenres();
  if (mode === "collections") return loadCollections();
  return loadTree();
}

// User picked a mode (tab or landing button): load it and drill in one level. Albums
// jumps straight to its grid (detail); Artists/Genres land on their index list.
function setBrowseMode(mode) {
  albumsDrilled = false;
  if (mode === "albums") { setLevel("detail"); loadMode(mode); return; }
  const noun = mode === "genres" ? "a genre" : mode === "collections" ? "a collection" : "an artist";
  detailEl.innerHTML = `<p class="muted">Select ${noun}.</p>`;
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
  // Albums has no left index list and its grid is edit-agnostic; only re-render the
  // artist page if we've drilled into one (preserving the grid's sort otherwise).
  if (browseMode === "albums") {
    if (CURRENT) selectArtist(CURRENT.path).then(() => restoreAnchor(anchor));
    return;
  }
  loadMode(browseMode).then(() => {
    if (CURRENT) selectArtist(CURRENT.path).then(() => restoreAnchor(anchor));
  });
}

// Remember which album section sits at the top of #detail (and its sub-offset), plus
// the raw scrollTop as a fallback, so a re-render that changes section heights
// (Browse↔Edit) or removes/renames the anchored album can still restore the view.
function captureAnchor() {
  if (!detailEl) return null;
  const scrollTop = detailEl.scrollTop;
  const cTop = detailEl.getBoundingClientRect().top;
  for (const sec of detailEl.querySelectorAll(".albumsection")) {
    const r = sec.getBoundingClientRect();
    if (r.bottom > cTop + 1) return { path: sec.dataset.path, delta: r.top - cTop, scrollTop };
  }
  return { scrollTop };   // no album sections in view
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
  setPlaceholder(indexEl, "Loading…");
  try {
    const data = await jget("/api/tree");
    TREE = data.artists;
    indexEl.innerHTML = "";
    for (const artist of TREE) indexEl.appendChild(artistEl(artist));
    if (!TREE.length) setPlaceholder(indexEl, "Empty library.");
  } catch (e) {
    setPlaceholder(indexEl, e.message, true);
  }
}

async function loadGenres() {
  setPlaceholder(indexEl, "Loading…");
  try {
    const data = await jget("/api/genres");
    GENRES = data.genres || [];
    indexEl.innerHTML = "";
    for (const g of GENRES) indexEl.appendChild(genreNodeEl(g));
    if (!GENRES.length) setPlaceholder(indexEl, "No genres.");
  } catch (e) {
    setPlaceholder(indexEl, e.message, true);
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

// ── Collections (owner-curated album groups; browse like Genres) ──────────────

async function loadCollections() {
  setPlaceholder(indexEl, "Loading…");
  try {
    const data = await jget("/api/collections");
    COLLECTIONS = data.collections || [];
    indexEl.innerHTML = "";
    const editable = isEdit() && !isBusy();
    if (editable) indexEl.appendChild(newCollectionButton());
    for (const c of COLLECTIONS) indexEl.appendChild(collectionNodeEl(c));
    if (!COLLECTIONS.length && !editable) setPlaceholder(indexEl, "No collections.");
  } catch (e) {
    setPlaceholder(indexEl, e.message, true);
  }
}

// Owner-only "create a collection" affordance at the top of the index (Edit mode).
function newCollectionButton() {
  const b = document.createElement("div");
  b.className = "node newcollection";
  b.innerHTML = `<span class="gname">＋ New collection</span>`;
  b.onclick = () => edit.createCollection(loadCollections);
  return b;
}

// A collection row: name + album-count badge. In Edit mode (owner) rename/delete glyphs.
function collectionNodeEl(c) {
  const head = document.createElement("div");
  head.className = "node genre collection";
  head.dataset.name = c.name;
  head.innerHTML =
    (isEdit() && !isBusy()
      ? `<span class="nodeact" data-act="rename" title="Rename collection">✎</span>` +
        `<span class="nodeact" data-act="del" title="Delete collection">🗑</span>` : "") +
    `<span class="gname">${escapeHtml(c.name)}</span><span class="gcount">${c.count}</span>`;
  head.onclick = (e) => {
    const act = e.target.dataset && e.target.dataset.act;
    if (act === "rename") { edit.renameCollection(c.name, loadCollections); return; }
    if (act === "del") { edit.deleteCollection(c.name, loadCollections); return; }
    showCollection(c.name, head);
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
    // Opening an album is strong play intent — start the transcode cache now
    // so playback begins on the bounded stream (no WAV interim).
    prewarmStream(path);
    const first = data.tracks[0] || {};
    return {
      path,
      tracks: data.tracks,
      artist: first.albumartist || first.artist || "",
      album: first.album || "",
      year: first.year || "",
      genre: first.genre || "",
    };
  } catch (e) { return null; }   // caller shows one summary toast per selection
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
      <div class="sub" id="artistSub">${artist.children.length} album${artist.children.length === 1 ? "" : "s"}</div>
    </div>
    <div id="artistAlbums"></div>`;
  wireBack();
  const host = detailEl.querySelector("#artistAlbums");
  if (!artist.children.length) { host.innerHTML = `<p class="muted">No albums.</p>`; return; }

  const states = await Promise.all(artist.children.map(a => fetchAlbumState(a.path)));
  if (!isCurrent("artist", path)) return;     // a newer selection won the race
  const failed = states.filter(st => !st).length;
  if (failed) toast(`${failed} album${failed === 1 ? "" : "s"} failed to load.`, true);

  // Now that track data is loaded, fold song count + total playtime into the header.
  const albumCount = states.filter(Boolean).length;
  const songCount = states.reduce((n, st) => n + (st ? st.tracks.length : 0), 0);
  const totalSec = states.reduce((s, st) =>
    s + (st ? st.tracks.reduce((a, t) => a + (Number(t.length_sec) || 0), 0) : 0), 0);
  const subParts = [`${albumCount} album${albumCount === 1 ? "" : "s"}`,
                    `${songCount} track${songCount === 1 ? "" : "s"}`];
  if (totalSec > 0) subParts.push(fmtDurationLong(totalSec));
  const subEl = detailEl.querySelector("#artistSub");
  if (subEl) subEl.textContent = subParts.join(" · ");
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

// Re-render the current artist page after an edit, preserving the scroll position
// (so deleting a track / applying art / etc. doesn't jump back to the top).
async function refreshCurrent() {
  if (!CURRENT) return;
  const anchor = captureAnchor();
  await selectArtist(CURRENT.path);
  restoreAnchor(anchor);
}

// ── Album grid (shared by Genres mode and Albums mode) ────────────────────────
// A sortable cover grid in the detail pane. Clicking a card reveals that album's
// artist page. Genres reach it by picking a genre; Albums shows every album at once.

// Genre grid: click a genre (or an album's genre link) to see same-genre albums.
async function showGenre(genre, headEl) {
  clearSel();
  if (headEl) headEl.classList.add("sel");
  gridName = genre;
  await showGrid("genre", `Genre · ${escapeHtml(genre)}`,
    "/api/genre?name=" + encodeURIComponent(genre), "No albums in this genre.");
}

// Albums grid: every album in the library.
async function showAlbums() {
  clearSel();
  await showGrid("albums", "All albums", "/api/albums", "No albums.");
}

// Collection grid: the albums curated into *name* (browsed like a genre). In Edit
// mode the grid gains an "Add albums" button and a per-card remove action.
async function showCollection(name, headEl, resetSort = true) {
  clearSel();
  if (headEl) headEl.classList.add("sel");
  gridName = name;
  await showGrid("collection", `Collection · ${escapeHtml(name)}`,
    "/api/collection?name=" + encodeURIComponent(name),
    "No albums in this collection yet.", resetSort);
}

// Fetch *url* → {albums} and render the sortable grid into #detail. *resetSort*
// is false on re-renders (mode/job toggles) so the user's sort choice survives.
async function showGrid(kind, heading, url, emptyMsg, resetSort = true) {
  CURRENT = null;     // a grid, not an artist — leave it alone on job/mode rerenders
  gridKind = kind;
  gridEmptyMsg = emptyMsg;
  if (resetSort) gridSort = "az";
  enterDetail();
  setPlaceholder(detailEl, "Loading…");
  let data;
  try { data = await jget(url); }
  catch (e) { toast(e.message, true); return; }
  gridAlbums = data.albums || [];
  detailEl.innerHTML = `
    ${BACK_BAR}
    <div class="artisthead genrehead">
      <h2>${heading}</h2>
      <div class="sub">${gridAlbums.length} album${gridAlbums.length === 1 ? "" : "s"}</div>
      <div class="sortbar">
        <span class="muted">Sort:</span>
        <button class="btn sortbtn" data-sort="az">A–Z</button>
        <button class="btn sortbtn" data-sort="date">Date</button>
        <button class="btn sortbtn" data-sort="rand">Random</button>
      </div>
    </div>
    <div class="genregrid" id="albumGrid"></div>`;
  wireBack();
  detailEl.querySelectorAll(".sortbtn").forEach(b =>
    b.onclick = () => { gridSort = b.dataset.sort; renderGrid(); });
  // Owner add-albums control lives next to the sort bar for collections.
  if (kind === "collection" && isEdit() && !isBusy()) {
    const head = detailEl.querySelector(".genrehead");
    const btn = document.createElement("button");
    btn.className = "btn addalbums";
    btn.textContent = "＋ Add albums";
    btn.onclick = () => edit.addAlbumsToCollection(
      gridName, gridAlbums.map(a => a.album_path), () => showCollection(gridName, null, false));
    head.appendChild(btn);
  }
  renderGrid();
}

// Re-show whichever grid is active (used by Back from a drilled-in artist page).
function reshowGrid() {
  if (gridKind === "albums") showGrid("albums", "All albums", "/api/albums", gridEmptyMsg, false);
  else if (gridKind === "collection") showCollection(gridName, null, false);
  else showGrid("genre", `Genre · ${escapeHtml(gridName)}`,
    "/api/genre?name=" + encodeURIComponent(gridName), gridEmptyMsg, false);
}

function sortedGridAlbums() {
  const list = gridAlbums.slice();
  if (gridSort === "az") {
    list.sort((a, b) => (a.album || "").localeCompare(b.album || "", undefined, { sensitivity: "base" }));
  } else if (gridSort === "date") {
    list.sort((a, b) => (a.year || "9999").localeCompare(b.year || "9999"));
  } else {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
  }
  return list;
}

function renderGrid() {
  const grid = detailEl.querySelector("#albumGrid");
  if (!grid) return;
  detailEl.querySelectorAll(".sortbtn").forEach(b =>
    b.classList.toggle("active", b.dataset.sort === gridSort));
  if (!gridAlbums.length) {
    setPlaceholder(grid, gridEmptyMsg);
    return;
  }
  const canRemove = gridKind === "collection" && isEdit() && !isBusy();
  grid.innerHTML = sortedGridAlbums().map(a => {
    // Cache-bust so a changed cover refreshes here too (matches the album head).
    const cover = "/api/cover?path=" + encodeURIComponent(a.album_path) + "&t=" + Date.now();
    const label = (a.album || "Untitled") + (a.artist ? " by " + a.artist : "");
    const rm = canRemove
      ? `<button class="gcardrm" title="Remove from collection" data-rm="${escapeAttr(a.album_path)}">✕</button>` : "";
    return `<div class="gcard" role="button" tabindex="0" aria-label="${escapeAttr(label)}"
                 data-album="${escapeAttr(a.album_path)}"
                 data-artist="${escapeAttr(a.artist_path)}" title="${escapeAttr((a.album || "") + " — " + (a.artist || ""))}">
        ${rm}<img src="${cover}" loading="lazy" alt="" onerror="this.style.visibility='hidden'">
        <div class="gcap"><b>${escapeHtml(a.album || "")}</b><span>${escapeHtml(a.artist || "")}</span></div>
      </div>`;
  }).join("");
  grid.querySelectorAll(".gcard").forEach(card => {
    const open = () => {
      // From the Albums grid, remember to return here on Back (vs the genre list).
      if (gridKind === "albums") albumsDrilled = true;
      applyReveal({
        artist_path: card.dataset.artist,
        album_path: card.dataset.album,
        track_path: null,
      });
    };
    card.onclick = open;
    card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  grid.querySelectorAll(".gcardrm").forEach(btn => btn.onclick = (e) => {
    e.stopPropagation();
    edit.removeAlbumFromCollection(gridName, btn.dataset.rm, () => showCollection(gridName, null, false));
  });
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

// "12 tracks · 48 minutes" (or just the count when no durations are available). Shared
// by the Browse and Edit renderers for the .albumtotals footer.
function albumMetaLine(tracks) {
  const totalSec = tracks.reduce((a, t) => a + (Number(t.length_sec) || 0), 0);
  const countLabel = `${tracks.length} track${tracks.length === 1 ? "" : "s"}`;
  return totalSec > 0 ? `${countLabel} · ${fmtDurationLong(totalSec)}` : countLabel;
}

// The album track table + totals footer. `cells(track)` renders each row's <td>s;
// `tableClass`/`rowClass` differ between Browse (playable) and Edit (draggable inputs).
// `extraHead` adds a trailing header cell (Edit uses it for the per-row delete column).
function trackTable(tracks, { tableClass = "", rowClass = "", extraHead = "" }, cells) {
  const rows = tracks.map(t => `
    <tr class="${rowClass}" data-path="${escapeAttr(t.path)}">${cells(t)}</tr>`).join("");
  return `
    <table${tableClass ? ` class="${tableClass}"` : ""}>
      <thead><tr><th>#</th><th>Title</th><th>Artist</th><th class="tdur">Time</th><th>Rate</th>${extraHead}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="albumtotals">${albumMetaLine(tracks)}</div>`;
}

// Shared trailing cells (Time + Rate) — identical in both modes.
function trackTailCells(t) {
  return `
      <td class="tdur muted">${escapeHtml(t.length || "")}</td>
      <td class="muted">${escapeHtml(t.bitrate ? t.bitrate + " kbps" : "")}</td>`;
}

function trackNum(t) {
  return escapeHtml((t.track || "").split("/")[0]);
}

function renderAlbumBrowseInto(container, st) {
  const { tracks, artist, album, year, genre } = st;
  const subParts = [escapeHtml(artist || "(unknown artist)")];
  if (year) subParts.push(escapeHtml(year));
  if (genre) subParts.push(`<span class="genrelink" role="button" tabindex="0" data-genre="${escapeAttr(genre)}">${escapeHtml(genre)}</span>`);
  const sub = subParts.join(" · ");
  container.innerHTML = albumHead(st, `
      <h2>${escapeHtml(album || "(untitled)")}</h2>
      <div class="sub">${sub}</div>`) +
    trackTable(tracks, { tableClass: "browsetable", rowClass: "browserow" }, t => `
      <td><span class="rowplay">▶</span> <span class="num">${trackNum(t)}</span></td>
      <td>${escapeHtml(t.title || "")}</td>
      <td>${escapeHtml(t.artist || "")}</td>${trackTailCells(t)}`);
  container.querySelectorAll("tr.browserow").forEach((tr, i) =>
    tr.onclick = () => playAlbum(tracks, i, st.path));
  container.querySelectorAll(".genrelink").forEach(el => {
    const open = (e) => { e.stopPropagation(); showGenre(el.dataset.genre); };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e); } };
  });
  updatePlayingHighlight(getCurrentPath());
}

function renderAlbumEditInto(container, st) {
  const { tracks, artist, album, year, genre } = st;
  container.innerHTML = albumHead(st, `
      <input class="hdr title" data-op="album_title" value="${escapeAttr(album)}" placeholder="Album title" aria-label="Album title">
      <div class="sub albumsub">
        <input class="hdr sub" data-op="album_artist" value="${escapeAttr(artist)}" placeholder="Album artist" aria-label="Album artist"> ·
        <input class="hdr sub" data-op="album_year" value="${escapeAttr(year)}" placeholder="Year" aria-label="Year"> ·
        <input class="hdr sub" data-op="album_genre" value="${escapeAttr(genre)}" placeholder="Genre" aria-label="Genre">
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" data-act="addcoll">Add to collection</button>
        <button class="btn danger" data-act="del">Delete album</button>
      </div>`) +
    trackTable(tracks, { rowClass: "editrow", extraHead: `<th class="trackact"></th>` }, t => `
      <td><span class="draghandle" title="Drag to reorder">⠿</span> <span class="num">${trackNum(t)}</span></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TIT2"
                 value="${escapeAttr(t.title || "")}" aria-label="Title"></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TPE1"
                 value="${escapeAttr(t.artist || "")}" aria-label="Artist"></td>${trackTailCells(t)}
      <td class="trackact"><button class="rowdel" title="Delete track" aria-label="Delete track"
                 data-del="${escapeAttr(t.path)}">🗑</button></td>`);

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
      if (res.ok && !res.error) toast("Saved.");
      else toast(res.error || "Save failed", true);
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

  container.querySelector('[data-act="addcoll"]').onclick = () =>
    edit.addAlbumToCollection(st.path);

  container.querySelector('[data-act="del"]').onclick = () =>
    edit.deleteAlbum(st.path, st.album || st.path.split("/").pop(), afterAlbumDelete);

  // Per-song delete (each confirms first). Deleting the album's last track prunes
  // the folder, so reload the tree in that case; otherwise just refresh the album.
  container.querySelectorAll(".rowdel").forEach(btn => btn.onclick = () => {
    const tr = btn.closest("tr");
    const title = (tr.querySelector('input[data-frame="TIT2"]').value.trim()) || "this track";
    edit.deleteTrack(btn.dataset.del, title,
      (res) => { if (res && res.album_deleted) afterAlbumDelete(); else refreshCurrent(); });
  });
}

// After deleting an album, reload the tree and re-select the artist — unless that
// was its last album (the artist folder gets pruned, so fall back to the placeholder).
// Preserve the scroll position (the deleted section is gone; the raw-scrollTop
// fallback keeps the remaining albums roughly in place instead of jumping to top).
async function afterAlbumDelete() {
  const artistPath = CURRENT && CURRENT.path;
  const anchor = captureAnchor();
  await loadTree();
  if (artistPath && TREE.find(a => a.path === artistPath)) {
    await selectArtist(artistPath);
    restoreAnchor(anchor);
  } else { CURRENT = null; detailEl.innerHTML = `<p class="muted">Select an artist or album.</p>`; }
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
    if (!res.ok || res.error) { toast(res.error || "Save failed", true); return; }
    toast(res.desc || "Saved.");
    const anchor = captureAnchor();
    await loadTree();
    if (CURRENT) { await selectArtist(CURRENT.path); restoreAnchor(anchor); }
  } catch (e) { toast(e.message, true); }
}

// ── Artwork search/apply (uses the shared modal) ──────────────────────────────

async function findArt(st) {
  const { artist, album } = st;
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
      card.innerHTML = `<img src="/api/art/thumb?url=${encodeURIComponent(r.thumb || r.url)}" loading="lazy">
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
