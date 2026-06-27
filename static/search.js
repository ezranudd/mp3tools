// Header search box: query /api/search, show a dropdown of albums + tracks.
// Clicking a result jumps to it in Browse; a ▶ on a track plays its album.
import { jget, toast, escapeHtml, escapeAttr } from "./util.js";
import { requestReveal } from "./tree.js";
import { playAlbum } from "./player.js";

let inputEl, panelEl, gotoView, timer = null;

export function initSearch(goto) {
  gotoView = goto;
  inputEl = document.getElementById("searchInput");
  panelEl = document.getElementById("searchResults");
  if (!inputEl) return;

  inputEl.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(run, 180);
  });
  inputEl.addEventListener("focus", () => { if (inputEl.value.trim()) run(); });
  inputEl.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
  document.addEventListener("click", e => {
    if (!e.target.closest("#search")) close();
  });
}

function close() { panelEl.classList.remove("show"); panelEl.innerHTML = ""; }

async function run() {
  const q = inputEl.value.trim();
  if (!q) { close(); return; }
  let data;
  try { data = await jget("/api/search?q=" + encodeURIComponent(q)); }
  catch (e) { toast(e.message, true); return; }
  if (inputEl.value.trim() !== q) return;   // a newer keystroke superseded this
  render(data);
}

function render({ albums, tracks }) {
  if (!albums.length && !tracks.length) {
    panelEl.innerHTML = `<div class="searchempty muted">No matches.</div>`;
    panelEl.classList.add("show");
    return;
  }
  let html = "";
  if (albums.length) {
    html += `<div class="searchgroup">Albums</div>`;
    html += albums.map(a => `
      <div class="searchrow" data-kind="album" data-artist="${escapeAttr(a.artist_path)}"
           data-album="${escapeAttr(a.path)}">
        <span>${escapeHtml(a.album)}</span>
        <span class="muted">${escapeHtml(a.artist)}</span>
      </div>`).join("");
  }
  if (tracks.length) {
    html += `<div class="searchgroup">Tracks</div>`;
    html += tracks.map(t => `
      <div class="searchrow" data-kind="track" data-artist="${escapeAttr(t.artist_path)}"
           data-album="${escapeAttr(t.album_path)}" data-track="${escapeAttr(t.path)}">
        <button class="rowplay" data-play title="Play">▶</button>
        <span>${escapeHtml(t.title)}</span>
        <span class="muted">${escapeHtml(t.artist)}${t.album ? " · " + escapeHtml(t.album) : ""}</span>
      </div>`).join("");
  }
  panelEl.innerHTML = html;
  panelEl.classList.add("show");

  panelEl.querySelectorAll(".searchrow").forEach(row => {
    row.onclick = () => {
      requestReveal({
        artist_path: row.dataset.artist,
        album_path: row.dataset.album,
        track_path: row.dataset.track || null,
      });
      close();
      inputEl.blur();
      gotoView("browse");
    };
    const play = row.querySelector("[data-play]");
    if (play) play.onclick = (e) => { e.stopPropagation(); playFrom(row.dataset.album, row.dataset.track); };
  });
}

async function playFrom(albumPath, trackPath) {
  try {
    const data = await jget("/api/album?path=" + encodeURIComponent(albumPath));
    const idx = Math.max(0, data.tracks.findIndex(t => t.path === trackPath));
    playAlbum(data.tracks, idx, albumPath);
  } catch (e) { toast(e.message, true); }
}
