// Browse view: library tree (left) + album detail (right) with inline + structural edits.
import { jget, jpost, toast, escapeHtml, escapeAttr } from "./util.js";
import * as edit from "./edit.js";

let CURRENT = null;   // { path, tracks, artist, album }
let treeEl, detailEl;

export async function show(container) {
  container.innerHTML = `<nav id="tree"></nav><section id="detail"><p class="muted">Select an album.</p></section>`;
  treeEl = container.querySelector("#tree");
  detailEl = container.querySelector("#detail");
  await loadTree();
}

async function loadTree() {
  treeEl.innerHTML = `<p class="muted" style="padding:10px">Loading…</p>`;
  try {
    const data = await jget("/api/tree");
    treeEl.innerHTML = "";
    for (const artist of data.artists) treeEl.appendChild(artistEl(artist));
    if (!data.artists.length) treeEl.innerHTML = `<p class="muted" style="padding:10px">Empty library.</p>`;
  } catch (e) {
    treeEl.innerHTML = `<p class="err" style="padding:10px">${escapeHtml(e.message)}</p>`;
  }
}

function artistEl(artist) {
  const wrap = document.createElement("div");
  const head = document.createElement("div");
  head.className = "node artist";
  head.innerHTML =
    `<span class="nodeact" title="Edit artist">✎</span>` +
    `<span class="caret">▸</span>${escapeHtml(artist.label)}`;
  const kids = document.createElement("div");
  kids.style.display = "none";
  for (const album of artist.children) kids.appendChild(albumEl(album));
  head.querySelector(".caret").parentElement.onclick = null;
  head.onclick = (e) => {
    if (e.target.classList.contains("nodeact")) {
      edit.editArtist(artist, loadTree);
      return;
    }
    const open = kids.style.display === "none";
    kids.style.display = open ? "block" : "none";
    head.querySelector(".caret").textContent = open ? "▾" : "▸";
  };
  wrap.append(head, kids);
  return wrap;
}

function albumEl(album) {
  const el = document.createElement("div");
  el.className = "node album";
  el.textContent = album.label;
  el.onclick = () => selectAlbum(album.path, el);
  return el;
}

async function selectAlbum(path, el) {
  document.querySelectorAll(".node.album.sel").forEach(n => n.classList.remove("sel"));
  if (el) el.classList.add("sel");
  try {
    const data = await jget("/api/album?path=" + encodeURIComponent(path));
    const first = data.tracks[0] || {};
    CURRENT = {
      path,
      tracks: data.tracks,
      artist: first.albumartist || first.artist || "",
      album: first.album || "",
      year: first.year || "",
      genre: first.genre || "",
    };
    renderAlbum();
  } catch (e) { toast(e.message, true); }
}

function renderAlbum() {
  const { path, tracks, artist, album, year, genre } = CURRENT;
  const cover = "/api/cover?path=" + encodeURIComponent(path) + "&t=" + Date.now();
  const rows = tracks.map(t => `
    <tr>
      <td class="num">${escapeHtml((t.track || "").split("/")[0])}</td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TIT2"
                 value="${escapeAttr(t.title || "")}"></td>
      <td><input class="tag" data-path="${escapeAttr(t.path)}" data-frame="TPE1"
                 value="${escapeAttr(t.artist || "")}"></td>
      <td class="muted">${escapeHtml(t.bitrate ? t.bitrate + " kbps" : "")}</td>
    </tr>`).join("");
  detailEl.innerHTML = `
    <div class="albumhead">
      <img class="cover" src="${cover}" onerror="this.style.visibility='hidden'">
      <div class="albummeta">
        <h2>${escapeHtml(album || "(untitled)")}</h2>
        <div class="sub">${escapeHtml(artist || "(unknown artist)")}${year ? " · " + escapeHtml(year) : ""}${genre ? " · " + escapeHtml(genre) : ""}</div>
        <div class="sub">${tracks.length} track${tracks.length === 1 ? "" : "s"}</div>
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn primary" data-act="save">Save tags</button>
          <button class="btn" data-act="title">Rename</button>
          <button class="btn" data-act="year">Year</button>
          <button class="btn" data-act="genre">Genre</button>
          <button class="btn" data-act="aartist">Album artist</button>
          <button class="btn" data-act="art">Find artwork</button>
          <button class="btn danger" data-act="rmart">Remove art</button>
        </div>
      </div>
    </div>
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Artist</th><th>Rate</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  detailEl.querySelectorAll("input.tag").forEach(inp => {
    inp._orig = inp.value;
    inp.oninput = () => inp.classList.toggle("dirty", inp.value !== inp._orig);
  });

  const reload = () => selectAlbum(CURRENT.path, document.querySelector(".node.album.sel"));
  const reloadTree = () => { loadTree(); detailEl.innerHTML = `<p class="muted">Select an album.</p>`; };
  const acts = {
    save: saveTags,
    title: () => edit.runEdit(path, "album_title", "Rename album", album, reloadTree),
    year: () => edit.runEdit(path, "album_year", "Album year", year, reloadTree),
    genre: () => edit.runEdit(path, "album_genre", "Album genre", genre, reload),
    aartist: () => edit.runEdit(path, "album_artist", "Move to album artist", artist, reloadTree),
    art: findArt,
    rmart: () => edit.removeArt(path, reload),
  };
  detailEl.querySelectorAll("[data-act]").forEach(b => b.onclick = () => acts[b.dataset.act]());
}

async function saveTags() {
  const dirty = [...detailEl.querySelectorAll("input.tag.dirty")];
  if (!dirty.length) { toast("No changes."); return; }
  const byPath = {};
  for (const inp of dirty) (byPath[inp.dataset.path] ||= {})[inp.dataset.frame] = inp.value;
  try {
    for (const [path, updates] of Object.entries(byPath)) await jpost("/api/tags", { path, updates });
    toast(`Saved ${dirty.length} change${dirty.length === 1 ? "" : "s"}.`);
    selectAlbum(CURRENT.path, document.querySelector(".node.album.sel"));
  } catch (e) { toast(e.message, true); }
}

// ── Artwork search/apply (uses the shared modal) ──────────────────────────────
async function findArt() {
  if (!CURRENT) return;
  const { artist, album } = CURRENT;
  const { openModal, closeModal } = await import("./util.js");
  openModal(`<h3>Artwork — ${escapeHtml(artist)} / ${escapeHtml(album)}</h3>
    <div id="artBody" class="grid"><p class="muted">Searching…</p></div>
    <div class="row"><button class="btn" data-close>Close</button></div>`,
    (box) => { box.querySelector("[data-close]").onclick = closeModal; });
  try {
    const data = await jget(`/api/art/search?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`);
    const results = data.results || [];
    const body = document.getElementById("artBody");
    if (!results.length) { body.innerHTML = `<p class="muted">No results.</p>`; return; }
    body.innerHTML = "";
    for (const r of results) {
      const card = document.createElement("div");
      card.className = "art";
      card.innerHTML = `<img src="${escapeAttr(r.url)}" loading="lazy">
        <div class="cap">${escapeHtml(r.source_label || r.source || "")}${r.size ? " · " + escapeHtml(r.size) : ""} · ${r.score ?? ""}</div>`;
      card.onclick = () => applyArt(r.url, closeModal);
      body.appendChild(card);
    }
  } catch (e) {
    const body = document.getElementById("artBody");
    if (body) body.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
  }
}

async function applyArt(url, close) {
  try {
    const res = await jpost("/api/art/apply", { path: CURRENT.path, url });
    close();
    toast(`Artwork applied (${res.updated} file${res.updated === 1 ? "" : "s"}).`);
    renderAlbum();
  } catch (e) { toast(e.message, true); }
}
