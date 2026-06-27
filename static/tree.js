// Browse view: library tree (left) + detail (right).
// Selecting an album shows that album; selecting an artist shows ALL its albums.
// Read-only in Browse mode; inline auto-saving fields in Edit mode.
import { jget, jpost, toast, escapeHtml, escapeAttr } from "./util.js";
import { isEdit, onModeChange } from "./mode.js";
import { isBusy, subscribeJob } from "./jobs.js";
import { playAlbum, subscribe as subscribePlayer, getCurrentPath } from "./player.js";
import * as edit from "./edit.js";

let CURRENT = null;   // selected artist: { kind: "artist", path }
let TREE = [];        // artist nodes from /api/tree
let treeEl, detailEl;
let subscribed = false;
let pendingReveal = null;   // { artist_path, album_path, track_path? } from search

// Ask Browse to jump to an album/track once it's (re)mounted.
export function requestReveal(target) { pendingReveal = target; }

export async function show(container) {
  container.innerHTML = `<nav id="tree"></nav><section id="detail"><p class="muted">Select an artist or album.</p></section>`;
  treeEl = container.querySelector("#tree");
  detailEl = container.querySelector("#detail");
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
  await loadTree();
  if (pendingReveal) {
    const target = pendingReveal;
    pendingReveal = null;
    applyReveal(target);
  }
}

async function applyReveal({ artist_path, album_path, track_path }) {
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
  loadTree().then(() => { if (CURRENT) selectArtist(CURRENT.path); });
}

async function loadTree() {
  treeEl.innerHTML = `<p class="muted" style="padding:10px">Loading…</p>`;
  try {
    const data = await jget("/api/tree");
    TREE = data.artists;
    treeEl.innerHTML = "";
    for (const artist of TREE) treeEl.appendChild(artistEl(artist));
    if (!TREE.length) treeEl.innerHTML = `<p class="muted" style="padding:10px">Empty library.</p>`;
  } catch (e) {
    treeEl.innerHTML = `<p class="err" style="padding:10px">${escapeHtml(e.message)}</p>`;
  }
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
  headEl = headEl || artistNodeEl(path);
  if (headEl) headEl.classList.add("sel");
  const artist = TREE.find(a => a.path === path);
  if (!artist) { detailEl.innerHTML = `<p class="muted">Artist not found.</p>`; return; }
  detailEl.innerHTML = `
    ${editPausedNotice()}
    <div class="artisthead">
      <h2>${escapeHtml(artist.label)}</h2>
      <div class="sub">${artist.children.length} album${artist.children.length === 1 ? "" : "s"}</div>
    </div>
    <div id="artistAlbums"></div>`;
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
  const sub = [artist || "(unknown artist)", year, genre].filter(Boolean).map(escapeHtml).join(" · ");
  const rows = tracks.map(t => `
    <tr class="browserow" data-path="${escapeAttr(t.path)}">
      <td><span class="rowplay">▶</span> <span class="num">${escapeHtml((t.track || "").split("/")[0])}</span></td>
      <td>${escapeHtml(t.title || "")}</td>
      <td>${escapeHtml(t.artist || "")}</td>
      <td class="muted">${escapeHtml(t.bitrate ? t.bitrate + " kbps" : "")}</td>
    </tr>`).join("");
  container.innerHTML = albumHead(st, `
      <h2>${escapeHtml(album || "(untitled)")}</h2>
      <div class="sub">${sub}</div>
      <div class="sub">${tracks.length} track${tracks.length === 1 ? "" : "s"}</div>`) + `
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Artist</th><th>Rate</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  container.querySelectorAll("tr.browserow").forEach((tr, i) =>
    tr.onclick = () => playAlbum(tracks, i));
  updatePlayingHighlight(getCurrentPath());
}

function renderAlbumEditInto(container, st) {
  const { tracks, artist, album, year, genre } = st;
  const rows = tracks.map(t => `
    <tr data-path="${escapeAttr(t.path)}">
      <td><button class="rowplay" data-play title="Play">▶</button> <span class="num">${escapeHtml((t.track || "").split("/")[0])}</span></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TIT2"
                 value="${escapeAttr(t.title || "")}"></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TPE1"
                 value="${escapeAttr(t.artist || "")}"></td>
      <td class="muted">${escapeHtml(t.bitrate ? t.bitrate + " kbps" : "")}</td>
    </tr>`).join("");
  container.innerHTML = albumHead(st, `
      <input class="hdr title" data-op="album_title" value="${escapeAttr(album)}" placeholder="Album title">
      <div class="sub albumsub">
        <input class="hdr sub" data-op="album_artist" value="${escapeAttr(artist)}" placeholder="Album artist"> ·
        <input class="hdr sub" data-op="album_year" value="${escapeAttr(year)}" placeholder="Year"> ·
        <input class="hdr sub" data-op="album_genre" value="${escapeAttr(genre)}" placeholder="Genre">
      </div>
      <div class="sub">${tracks.length} track${tracks.length === 1 ? "" : "s"}</div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" data-act="art">Find artwork</button>
        <button class="btn danger" data-act="rmart">Remove art</button>
      </div>`) + `
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Artist</th><th>Rate</th></tr></thead>
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

  // Play buttons (editing stays in the title/artist cells).
  container.querySelectorAll("button[data-play]").forEach((b, i) =>
    b.onclick = (e) => { e.stopPropagation(); playAlbum(tracks, i); });
  updatePlayingHighlight(getCurrentPath());

  container.querySelector('[data-act="art"]').onclick = () => findArt(st);
  container.querySelector('[data-act="rmart"]').onclick = () => edit.removeArt(st.path, refreshCurrent);
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
  inp.style.width = (_measureEl.offsetWidth + 4) + "px";   // +4 for the caret
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
    <div class="row"><button class="btn" data-close>Close</button></div>`,
    (box) => { box.querySelector("[data-close]").onclick = closeModal; });
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
