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

const coverThumb = (albumPath) =>
  `<img class="searchthumb" src="/api/cover?path=${encodeURIComponent(albumPath)}"
        onerror="this.classList.add('noart')">`;

function render({ artists, albums, tracks }) {
  artists = artists || [];
  if (!artists.length && !albums.length && !tracks.length) {
    panelEl.innerHTML = `<div class="searchempty muted">No matches.</div>`;
    panelEl.classList.add("show");
    return;
  }
  let html = "";
  if (artists.length) {
    html += `<div class="searchgroup">Artists</div>`;
    html += artists.map(a => `
      <div class="searchrow" data-kind="artist" data-artist="${escapeAttr(a.artist_path)}">
        <span class="searchthumb glyph"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8.6" r="4.8"/><path d="M12 14.6c-4.7 0-8.5 3-8.5 6.8V24h17v-2.6c0-3.8-3.8-6.8-8.5-6.8z"/></svg></span>
        <span>${escapeHtml(a.artist)}</span>
        <span class="muted">${a.n_albums} album${a.n_albums === 1 ? "" : "s"}</span>
      </div>`).join("");
  }
  if (albums.length) {
    html += `<div class="searchgroup">Albums</div>`;
    html += albums.map(a => `
      <div class="searchrow" data-kind="album" data-artist="${escapeAttr(a.artist_path)}"
           data-album="${escapeAttr(a.path)}">
        ${coverThumb(a.path)}
        <span>${escapeHtml(a.album)}</span>
        <span class="muted">${escapeHtml(a.artist)}</span>
      </div>`).join("");
  }
  if (tracks.length) {
    html += `<div class="searchgroup">Tracks</div>`;
    html += tracks.map(t => `
      <div class="searchrow" data-kind="track" data-artist="${escapeAttr(t.artist_path)}"
           data-album="${escapeAttr(t.album_path)}" data-track="${escapeAttr(t.path)}">
        ${coverThumb(t.album_path)}
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
