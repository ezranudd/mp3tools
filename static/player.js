// Persistent gapless player. Lives in #player (sibling of #view) so it survives
// navigation. Playback runs through the Web Audio API: each track is decoded to an
// AudioBuffer and the next one is scheduled to start at the exact sample the current
// ends (using the AudioContext clock), so albums play with no gap. MP3 encoder
// delay/padding is parsed from the Xing/Info+LAME header and trimmed, since
// decodeAudioData (Chromium) keeps that baked-in silence.
let bar = null;
let queue = [];          // [{ path, title, artist, track }]
let index = -1;
let currentAlbumPath = null;   // album dir of the playing queue, for "jump to album"
let reveal = null;             // callback to show the playing album in Browse
const els = {};
const subs = new Set();

// ── Web Audio engine state ──────────────────────────────────────────────────
let ctx = null;
let gain = null;
const cache = new Map();   // path → { buffer, delaySec, durationSec }
let gen = 0;               // bumped on any user action to invalidate stale async work
let curSource = null;
let nextSource = null;
let nextStart = 0;         // ctx time the scheduled next track begins
let nextMeta = null;
let startCtxTime = 0;      // ctx time at which the current track's offset 0 plays
let startOffset = 0;       // track position (s) at startCtxTime
let duration = 0;          // trimmed duration (s) of the current track
let playing = false;
let rafId = 0;

// Transport icons as inline SVG (24×24, fill:currentColor) — uniform size/baseline
// across play/pause/skip, unlike the Unicode media glyphs.
const SVG = (body) => `<svg viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
const ICON = {
  prev: SVG(`<path d="M6 6h2v12H6z M20 6v12l-10.5-6z"/>`),
  next: SVG(`<path d="M16 6h2v12h-2z M4 6l10.5 6L4 18z"/>`),
  play: SVG(`<path d="M8 5v14l11-7z"/>`),
  pause: SVG(`<path d="M7 5h3.5v14H7z M13.5 5H17v14h-3.5z"/>`),
};

export function getCurrentPath() {
  return index >= 0 && queue[index] ? queue[index].path : null;
}
export function getCurrentAlbumPath() {
  return index >= 0 ? currentAlbumPath : null;
}
export function subscribe(fn) { subs.add(fn); fn(getCurrentPath()); return () => subs.delete(fn); }
function notify() { const p = getCurrentPath(); for (const fn of subs) fn(p); }

export function initPlayer(revealFn = null) {
  reveal = revealFn;
  bar = document.getElementById("player");

  bar.innerHTML = `
    <button class="pbtn" data-prev title="Previous">${ICON.prev}</button>
    <button class="pbtn play" data-play title="Play/Pause">${ICON.play}</button>
    <button class="pbtn" data-next title="Next">${ICON.next}</button>
    <span class="ptitle" data-title title="Show in library"></span>
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
  els.title.onclick = jumpToAlbum;
  els.play.onclick = toggle;

  els.seek.oninput = () => { if (duration) seek(els.seek.value / 1000); };
}

function ensureCtx() {
  if (ctx) return ctx;
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    gain = ctx.createGain();
    gain.connect(ctx.destination);
  } catch { ctx = null; }
  return ctx;
}

export function playAlbum(tracks, startIndex = 0, albumPath = null) {
  queue = (tracks || []).map(t => ({
    path: t.path,
    title: t.title || t.label || t.path,
    artist: t.artist || "",
    track: (t.track || "").split("/")[0],
  }));
  currentAlbumPath = albumPath;
  if (!ensureCtx()) return;
  startPlayback(startIndex, 0);
}

// Jump to the playing album in Browse (wired via initPlayer's reveal callback).
function jumpToAlbum() {
  if (!currentAlbumPath || !reveal) return;
  reveal({ album_path: currentAlbumPath, track_path: getCurrentPath() });
}

// ── Decode + gapless metadata ────────────────────────────────────────────────

async function load(path) {
  const hit = cache.get(path);
  if (hit) return hit;
  const resp = await fetch("/api/track?path=" + encodeURIComponent(path));
  const raw = await resp.arrayBuffer();
  // Parse BEFORE decodeAudioData — it detaches the ArrayBuffer.
  const g = parseGapless(new Uint8Array(raw));
  const buffer = await ctx.decodeAudioData(raw);
  const delaySec = g.sampleRate ? g.delaySamples / g.sampleRate : 0;
  const padSec = g.sampleRate ? g.paddingSamples / g.sampleRate : 0;
  const durationSec = Math.max(0.01, buffer.duration - delaySec - padSec);
  const meta = { buffer, delaySec, durationSec };
  cache.set(path, meta);
  return meta;
}

function evictBuffers() {
  const keep = new Set([queue[index - 1], queue[index], queue[index + 1]]
    .filter(Boolean).map(t => t.path));
  for (const k of [...cache.keys()]) if (!keep.has(k)) cache.delete(k);
}

const _MPEG_RATES = {
  // [version bits][sample-rate bits] — version: 3=MPEG1, 2=MPEG2, 0=MPEG2.5
  3: [44100, 48000, 32000],
  2: [22050, 24000, 16000],
  0: [11025, 12000, 8000],
};

// Best-effort Xing/Info + LAME gapless reader. Returns {delaySamples,
// paddingSamples, sampleRate}; zeros (no trim) on anything unexpected.
function parseGapless(b) {
  const none = { delaySamples: 0, paddingSamples: 0, sampleRate: 0 };
  try {
    let o = 0;
    if (b[0] === 0x49 && b[1] === 0x44 && b[2] === 0x33) {   // "ID3" v2
      const size = (b[6] << 21) | (b[7] << 14) | (b[8] << 7) | b[9];   // syncsafe
      o = 10 + size;
    }
    if (b[o] !== 0xff || (b[o + 1] & 0xe0) !== 0xe0) return none;      // frame sync
    const verBits = (b[o + 1] >> 3) & 0x03;     // 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    const chanBits = (b[o + 3] >> 6) & 0x03;    // 3=mono
    const rateBits = (b[o + 2] >> 2) & 0x03;
    const rates = _MPEG_RATES[verBits];
    if (!rates || rateBits === 3) return none;
    const sampleRate = rates[rateBits];

    // Xing/Info tag sits after the side-info block, whose size depends on
    // MPEG version and channel mode.
    const mono = chanBits === 3;
    const sideInfo = verBits === 3 ? (mono ? 17 : 32) : (mono ? 9 : 17);
    const p = o + 4 + sideInfo;
    const tag = String.fromCharCode(b[p], b[p + 1], b[p + 2], b[p + 3]);
    if (tag !== "Xing" && tag !== "Info") return none;

    // Skip the Xing header's variable-length fields to reach the LAME tag, whose
    // 3 gapless bytes (12-bit delay + 12-bit padding) sit at LAME offset 21.
    const flags = (b[p + 4] << 24) | (b[p + 5] << 16) | (b[p + 6] << 8) | b[p + 7];
    let lame = p + 8;
    if (flags & 1) lame += 4;     // frame count
    if (flags & 2) lame += 4;     // byte count
    if (flags & 4) lame += 100;   // TOC
    if (flags & 8) lame += 4;     // quality
    const g = lame + 21;
    const delaySamples = (b[g] << 4) | (b[g + 1] >> 4);
    const paddingSamples = ((b[g + 1] & 0x0f) << 8) | b[g + 2];
    // Decoder priming adds 528+1 samples beyond the stored encoder delay.
    return { delaySamples: delaySamples + 529, paddingSamples: Math.max(0, paddingSamples - 529), sampleRate };
  } catch {
    return none;
  }
}

// ── Scheduling ───────────────────────────────────────────────────────────────

function makeSource(buffer, token) {
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(gain);
  src._gen = gen;
  src._token = token;
  src._natural = true;
  src.onended = onSourceEnded;
  return src;
}

function stopSource(src) {
  if (!src) return;
  try { src._natural = false; src.stop(); } catch { /* not started */ }
  try { src.disconnect(); } catch { /* ignore */ }
}

function clearNext() {
  if (nextSource) { stopSource(nextSource); nextSource = null; nextMeta = null; }
}

// Start the current track at musical `offset`, beginning at ctx time `when`.
function scheduleCurrent(meta, offset, when) {
  curSource = makeSource(meta.buffer, index);
  duration = meta.durationSec;
  startCtxTime = when;
  startOffset = offset;
  const endIn = duration - offset;
  curSource.start(when, meta.delaySec + offset);
  curSource.stop(when + endIn);          // trim end padding
}

// Pre-schedule the next track to begin exactly when the current ends.
function scheduleNext() {
  clearNext();
  const nextTrack = queue[index + 1];
  if (!nextTrack) return;
  const myGen = gen;
  const endCtxTime = startCtxTime + (duration - startOffset);
  load(nextTrack.path).then(meta => {
    if (gen !== myGen) return;   // a newer action superseded this
    // Scheduling while the context is suspended (paused) is fine: the node fires
    // when the clock resumes, at the same absolute ctx time.
    nextSource = makeSource(meta.buffer, index + 1);
    nextMeta = meta;
    nextStart = endCtxTime;
    nextSource.start(endCtxTime, meta.delaySec);
  }).catch(() => {});
}

function onSourceEnded(e) {
  const src = e.target;
  if (!src._natural || src !== curSource) return;   // manual stop or stale node
  if (nextSource && queue[index + 1]) {
    // Gapless handoff: the next node is already playing.
    index += 1;
    curSource = nextSource;
    const meta = nextMeta;
    nextSource = null; nextMeta = null;
    startCtxTime = nextStart;
    startOffset = 0;
    duration = meta.durationSec;
    curSource.stop(startCtxTime + duration);   // trim its end padding
    reflectTrack();
    evictBuffers();
    scheduleNext();
  } else {
    curSource = null;          // end of album — allow play to restart this track
    playing = false;
    els.play.innerHTML = ICON.play;
    stopRaf();
  }
}

async function startPlayback(i, offset) {
  if (i < 0 || i >= queue.length || !ensureCtx()) return;
  gen += 1;
  const myGen = gen;
  stopSource(curSource); curSource = null;
  clearNext();
  if (ctx.state === "suspended") ctx.resume();
  index = i;
  let meta;
  try { meta = await load(queue[i].path); } catch { return; }
  if (gen !== myGen) return;            // a newer action superseded this load
  playing = true;
  scheduleCurrent(meta, offset, ctx.currentTime + 0.05);
  bar.classList.add("show");
  reflectTrack();
  els.play.innerHTML = ICON.pause;
  evictBuffers();
  scheduleNext();
  startRaf();
}

// ── Transport ────────────────────────────────────────────────────────────────

export function toggle() {
  if (index < 0 || !ctx) return;
  if (playing) {
    ctx.suspend();
    playing = false;
    els.play.innerHTML = ICON.play;
    stopRaf();
  } else if (!curSource) {
    startPlayback(index, startOffset || 0);   // ended/seeked-empty → (re)start
  } else {
    ctx.resume();
    playing = true;
    els.play.innerHTML = ICON.pause;
    startRaf();
  }
}

export function next() {
  if (index < queue.length - 1) startPlayback(index + 1, 0);
}

export function prev() {
  if (elapsed() > 3 || index <= 0) seek(0);
  else startPlayback(index - 1, 0);
}

function seek(frac) {
  if (index < 0 || duration <= 0) return;
  const meta = cache.get(queue[index].path);
  if (!meta) return;
  gen += 1;
  const offset = Math.max(0, Math.min(1, frac)) * duration;
  stopSource(curSource); curSource = null;
  clearNext();
  // Reschedule from the new offset. When paused the context stays suspended, so
  // the (re)scheduled nodes simply wait until toggle() resumes the clock.
  scheduleCurrent(meta, offset, ctx.currentTime + 0.02);
  scheduleNext();
  updateProgress();
}

// ── Progress UI ────────────────────────────────────────────────────────────

function elapsed() {
  if (index < 0) return 0;
  const e = playing ? startOffset + (ctx.currentTime - startCtxTime) : startOffset;
  return Math.max(0, Math.min(duration, e));
}

function updateProgress() {
  const e = elapsed();
  els.seek.value = duration ? String(Math.round((e / duration) * 1000)) : "0";
  els.cur.textContent = fmt(e);
  els.dur.textContent = fmt(duration);
}

function startRaf() {
  stopRaf();
  const tick = () => { updateProgress(); rafId = requestAnimationFrame(tick); };
  rafId = requestAnimationFrame(tick);
}
function stopRaf() { if (rafId) cancelAnimationFrame(rafId); rafId = 0; }

function reflectTrack() {
  const t = queue[index];
  const num = t.track ? t.track.padStart(2, "0") + ". " : "";
  els.title.textContent = num + t.title + (t.artist ? " — " + t.artist : "");
  updateProgress();
  notify();
}

function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}
