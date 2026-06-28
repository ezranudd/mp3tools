// Persistent gapless player. Lives in #player (sibling of #view) so it survives
// navigation.
//
// Two playback backends, chosen once at init:
//   • Desktop — the Web Audio API. Each track is decoded to an AudioBuffer and the
//     next is scheduled to start at the exact sample the current ends (using the
//     AudioContext clock), so albums play with no gap. MP3 encoder delay/padding is
//     parsed from the Xing/Info+LAME header and trimmed, since decodeAudioData keeps
//     that baked-in silence.
//   • Mobile (IS_MOBILE) — a plain <audio> element. Web Audio on iOS is muted by the
//     hardware silent switch and has no lock-screen controls; an <audio> element
//     plays through the media channel, supports MediaSession, and streams via Range
//     (no full in-memory decode). Gapless is sacrificed on phones (a small gap at
//     track boundaries). The queue/transport/UI below is shared; only the sound
//     backend differs, branched at startPlayback/toggle/seek/elapsed.
import { toast } from "./util.js";

// Phones/tablets: touch, no hover. These get the <audio> backend.
const IS_MOBILE = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
let mAudio = null;             // HTMLAudioElement (mobile backend only)
let mWantPos = 0;              // position (s) to restore to after a reload
let mRetries = 0;              // reloads attempted for the current track
let mWatchdog = 0;             // setInterval id watching for a stalled stream
let mLastTime = 0;             // last observed currentTime, for stall detection
const M_MAX_RETRIES = 3;

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
    <img class="palbum noart" data-album alt="" title="Show in library"
         onerror="this.classList.add('noart')">
    <div class="pmeta">
      <span class="ptitle" data-title></span>
      <span class="partist" data-artist></span>
    </div>
    <span class="pbitrate" data-bitrate></span>
    <div class="pseekrow">
      <span class="ptime" data-cur>0:00</span>
      <input type="range" class="pseek" data-seek min="0" max="1000" value="0">
      <span class="ptime" data-dur>0:00</span>
    </div>`;

  els.play = bar.querySelector("[data-play]");
  els.album = bar.querySelector("[data-album]");
  els.title = bar.querySelector("[data-title]");
  els.artist = bar.querySelector("[data-artist]");
  els.cur = bar.querySelector("[data-cur]");
  els.dur = bar.querySelector("[data-dur]");
  els.bitrate = bar.querySelector("[data-bitrate]");
  els.seek = bar.querySelector("[data-seek]");

  bar.querySelector("[data-prev]").onclick = prev;
  bar.querySelector("[data-next]").onclick = next;
  // Title/artist are plain text; the album art is the "jump to album" affordance.
  els.album.onclick = jumpToAlbum;
  els.play.onclick = toggle;

  els.seek.oninput = () => { if (curDuration()) seek(els.seek.value / 1000); };

  if (IS_MOBILE) initElementBackend();
}

// ── Mobile <audio> backend ────────────────────────────────────────────────────

function initElementBackend() {
  mAudio = new Audio();
  mAudio.preload = "auto";
  mAudio.setAttribute("playsinline", "");
  mAudio.style.display = "none";
  bar.appendChild(mAudio);
  mAudio.onended = () => {
    stopWatchdog();
    if (index < queue.length - 1) startPlayback(index + 1, 0);
    else { playing = false; els.play.innerHTML = ICON.play; stopRaf(); updateMediaState(); }
  };
  // A dropped connection (backgrounded PWA, slept radio) surfaces here or as a
  // silent stall. Reload the source on a fresh connection rather than giving up;
  // only surface an error once the retry budget is spent.
  mAudio.onerror = () => mRecover();
  mAudio.addEventListener("playing", () => { mLastTime = mAudio.currentTime; });
  mAudio.addEventListener("timeupdate", () => { mLastTime = mAudio.currentTime; });

  applyMediaHandlers();
}

// Register ONLY play/pause/prev/next — and NOTHING seek-related (no seekto,
// seekbackward, seekforward, or setPositionState). On iOS/WebKit any seek/position
// signal makes the lock screen prefer the ±10s skip UI over previous/next TRACK
// buttons. Re-asserted on every track (updateMediaSession) in case iOS drops the
// handlers when the media item changes. Wrap: unsupported actions throw.
function applyMediaHandlers() {
  if (!("mediaSession" in navigator)) return;
  const ms = navigator.mediaSession;
  const setH = (action, handler) => { try { ms.setActionHandler(action, handler); } catch { /* unsupported */ } };
  setH("play", () => { if (!playing) toggle(); });
  setH("pause", () => { if (playing) toggle(); });
  setH("previoustrack", prev);
  setH("nexttrack", next);
}

// Play queue[i] from `offset` seconds through the <audio> element.
function mStart(i, offset) {
  index = i;
  playing = true;
  mWantPos = offset || 0;
  mRetries = 0;
  bar.classList.add("show");
  mLoadSrc(offset || 0);
  reflectTrack();
  els.play.innerHTML = ICON.pause;
  startRaf();
  startWatchdog();
}

// (Re)point the <audio> element at the current track and start it at `offset`.
// `bust` adds a cache-busting query so iOS opens a NEW connection on a retry
// instead of reusing the dead socket that just failed.
function mLoadSrc(offset, bust = 0) {
  let src = "/api/track?path=" + encodeURIComponent(queue[index].path);
  if (bust) src += "&_r=" + bust;
  mAudio.src = src;
  mLastTime = 0;
  if (offset) {
    mAudio.addEventListener("loadedmetadata",
      () => { try { mAudio.currentTime = offset; } catch { /* ignore */ } }, { once: true });
  }
  const p = mAudio.play();
  if (p) p.catch(err => {
    // Autoplay rejections (user-gesture) are surfaced; transient load failures
    // are handled by the error/watchdog recovery path.
    if (err && err.name !== "AbortError") toast("Playback failed: " + (err.message || err), true);
  });
}

// Reload the current track on a fresh connection after a drop/stall.
function mRecover() {
  if (!playing) return;                 // user paused/stopped — nothing to recover
  if (mRetries >= M_MAX_RETRIES) {
    stopWatchdog();
    toast("Could not play this track", true);
    return;
  }
  mRetries += 1;
  mWantPos = mAudio.currentTime || mWantPos;
  mLoadSrc(mWantPos, mRetries);
}

// Watch for a silent stall: if we should be playing but currentTime hasn't moved
// for ~10s, the stream is wedged — recover it.
function startWatchdog() {
  stopWatchdog();
  let stuckSince = 0;
  mWatchdog = setInterval(() => {
    if (!playing || !mAudio) return;
    if (mAudio.currentTime > mLastTime + 0.01 || mAudio.paused) { stuckSince = 0; return; }
    if (!stuckSince) { stuckSince = Date.now(); return; }
    if (Date.now() - stuckSince >= 10000) { stuckSince = 0; mRecover(); }
  }, 2000);
}

function stopWatchdog() {
  if (mWatchdog) { clearInterval(mWatchdog); mWatchdog = 0; }
}

function mToggle() {
  if (index < 0) return;
  if (playing) {
    mAudio.pause();
    playing = false;
    els.play.innerHTML = ICON.play;
    stopRaf();
  } else if (mAudio.ended || !mAudio.src) {
    startPlayback(index, 0);          // end-of-track → restart
  } else {
    mAudio.play().catch(() => {});
    playing = true;
    els.play.innerHTML = ICON.pause;
    startRaf();
  }
  updateMediaState();
}

// Mirror the current track to the OS (lock screen / Control Center).
function updateMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const t = queue[index];
  if (!t) return;
  const artwork = currentAlbumPath
    ? [{ src: "/api/cover?path=" + encodeURIComponent(currentAlbumPath),
         sizes: "500x500", type: "image/jpeg" }]
    : [];
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title || "", artist: t.artist || "", album: "", artwork });
  } catch { /* MediaMetadata unsupported */ }
  // Deliberately NO setPositionState — a seekable position hint makes iOS show the
  // ±10s skip controls instead of next/prev track. Re-assert the track handlers in
  // case iOS cleared them when the media item changed.
  applyMediaHandlers();
  updateMediaState();
}

function updateMediaState() {
  if ("mediaSession" in navigator) {
    navigator.mediaSession.playbackState = playing ? "playing" : "paused";
  }
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
    bitrate: t.bitrate || "",
  }));
  currentAlbumPath = albumPath;
  if (!IS_MOBILE && !ensureCtx()) return;
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
  if (i < 0 || i >= queue.length) return;
  if (IS_MOBILE) { mStart(i, offset); return; }
  if (!ensureCtx()) return;
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
  if (IS_MOBILE) { mToggle(); return; }
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
  if (IS_MOBILE) {
    if (mAudio && mAudio.duration) {
      mAudio.currentTime = Math.max(0, Math.min(1, frac)) * mAudio.duration;
      updateProgress();
    }
    return;
  }
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

// Trimmed duration (s) of the current track, from whichever backend is active.
function curDuration() {
  return IS_MOBILE ? (mAudio && mAudio.duration ? mAudio.duration : 0) : duration;
}

function elapsed() {
  if (index < 0) return 0;
  if (IS_MOBILE) return mAudio ? (mAudio.currentTime || 0) : 0;
  const e = playing ? startOffset + (ctx.currentTime - startCtxTime) : startOffset;
  return Math.max(0, Math.min(duration, e));
}

function updateProgress() {
  const e = elapsed();
  const d = curDuration();
  els.seek.value = d ? String(Math.round((e / d) * 1000)) : "0";
  els.cur.textContent = fmt(e);
  els.dur.textContent = fmt(d);
}

function startRaf() {
  stopRaf();
  const tick = () => { updateProgress(); rafId = requestAnimationFrame(tick); };
  rafId = requestAnimationFrame(tick);
}
function stopRaf() { if (rafId) cancelAnimationFrame(rafId); rafId = 0; }

// Scroll an overflowing label back and forth (mobile player) so the full text is
// readable, instead of truncating. Falls back to the CSS ellipsis when it fits or
// when the user prefers reduced motion.
function applyMarquee(el) {
  el.classList.remove("marquee");
  el.style.removeProperty("--mq-shift");
  el.style.removeProperty("--mq-dur");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  requestAnimationFrame(() => {
    const overflow = el.scrollWidth - el.clientWidth;
    if (overflow <= 4) return;
    el.style.setProperty("--mq-shift", `-${overflow}px`);
    el.style.setProperty("--mq-dur", `${Math.max(6, overflow / 30 + 3).toFixed(1)}s`);
    el.classList.add("marquee");
  });
}

function reflectTrack() {
  const t = queue[index];
  if (IS_MOBILE) {
    // Phone bar shows the song title over the artist (no track number); .partist
    // is hidden on desktop via CSS.
    els.title.textContent = t.title || "";
    els.artist.textContent = t.artist || "";
    applyMarquee(els.title);
    applyMarquee(els.artist);
  } else {
    const num = t.track ? t.track.padStart(2, "0") + ". " : "";
    els.title.textContent = num + t.title + (t.artist ? " — " + t.artist : "");
  }
  els.bitrate.textContent = t.bitrate ? t.bitrate + " kbps" : "";
  if (currentAlbumPath) {
    els.album.src = "/api/cover?path=" + encodeURIComponent(currentAlbumPath);
    els.album.classList.remove("noart");
  } else {
    els.album.classList.add("noart");
  }
  updateProgress();
  if (IS_MOBILE) updateMediaSession();
  notify();
}

function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}
