// Persistent audio player: one <audio> in the bottom bar, an album queue that
// auto-advances. Lives in #player (sibling of #view) so it survives navigation.
let audio = null;
let bar = null;
let queue = [];          // [{ path, title, artist, track }]
let index = -1;
const els = {};
const subs = new Set();

export function getCurrentPath() {
  return index >= 0 && queue[index] ? queue[index].path : null;
}
export function subscribe(fn) { subs.add(fn); fn(getCurrentPath()); return () => subs.delete(fn); }
function notify() { const p = getCurrentPath(); for (const fn of subs) fn(p); }

export function initPlayer() {
  bar = document.getElementById("player");
  audio = new Audio();

  bar.innerHTML = `
    <button class="pbtn" data-prev title="Previous">⏮</button>
    <button class="pbtn play" data-play title="Play/Pause">▶</button>
    <button class="pbtn" data-next title="Next">⏭</button>
    <span class="ptitle" data-title></span>
    <span class="ptime" data-cur>0:00</span>
    <input type="range" class="pseek" data-seek min="0" max="1000" value="0">
    <span class="ptime" data-dur>0:00</span>`;

  els.play = bar.querySelector("[data-play]");
  els.title = bar.querySelector("[data-title]");
  els.cur = bar.querySelector("[data-cur]");
  els.dur = bar.querySelector("[data-dur]");
  els.seek = bar.querySelector("[data-seek]");

  bar.querySelector("[data-prev]").onclick = prev;
  bar.querySelector("[data-next]").onclick = next;
  els.play.onclick = toggle;

  els.seek.oninput = () => {
    if (audio.duration) audio.currentTime = (els.seek.value / 1000) * audio.duration;
  };

  audio.addEventListener("timeupdate", () => {
    if (!audio.duration) return;
    els.seek.value = String(Math.round((audio.currentTime / audio.duration) * 1000));
    els.cur.textContent = fmt(audio.currentTime);
  });
  audio.addEventListener("loadedmetadata", () => { els.dur.textContent = fmt(audio.duration); });
  audio.addEventListener("play", () => { els.play.textContent = "⏸"; });
  audio.addEventListener("pause", () => { els.play.textContent = "▶"; });
  audio.addEventListener("ended", next);
}

export function playAlbum(tracks, startIndex = 0) {
  queue = (tracks || []).map(t => ({
    path: t.path,
    title: t.title || t.label || t.path,
    artist: t.artist || "",
    track: (t.track || "").split("/")[0],
  }));
  playIndex(startIndex);
}

function playIndex(i) {
  if (i < 0 || i >= queue.length) return;
  index = i;
  const t = queue[index];
  audio.src = "/api/track?path=" + encodeURIComponent(t.path);
  audio.play().catch(() => {});
  bar.classList.add("show");
  const num = t.track ? t.track.padStart(2, "0") + ". " : "";
  els.title.textContent = num + t.title + (t.artist ? " — " + t.artist : "");
  notify();
}

export function toggle() {
  if (index < 0) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}
export function next() {
  if (index < queue.length - 1) playIndex(index + 1);
  else els.play.textContent = "▶";   // end of album
}
export function prev() {
  if (audio.currentTime > 3 || index <= 0) audio.currentTime = 0;
  else playIndex(index - 1);
}

function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}
