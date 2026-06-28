// Optional appearance feature: tint the theme to the playing album's art.
// While enabled, the highlight (--accent) and a subtle wash over --bg/--panel/--line
// are derived from the current cover. Text colors are left to the theme for legibility.
import { jget } from "./util.js";
import { subscribe, getCurrentAlbumPath } from "./player.js";

let enabled = false;
let lastRGB = null;     // [r,g,b] extracted from the current cover, for theme re-derive
let baseColor = null;   // [r,g,b] user-chosen fixed accent, or null = theme default

// ── Colour helpers ────────────────────────────────────────────────────────────

function parseColor(s) {
  s = (s || "").trim();
  if (s.startsWith("#")) {
    const h = s.slice(1);
    const n = h.length === 3
      ? h.split("").map(c => parseInt(c + c, 16))
      : [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    return n.some(Number.isNaN) ? null : n;
  }
  const m = s.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(",").map(x => parseFloat(x));
    return [p[0], p[1], p[2]];
  }
  return null;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const hex2 = v => clamp(Math.round(v), 0, 255).toString(16).padStart(2, "0");
const toHex = ([r, g, b]) => `#${hex2(r)}${hex2(g)}${hex2(b)}`;

function rgb2hsl([r, g, b]) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60; if (h < 0) h += 360;
  }
  const l = (max + min) / 2;
  const s = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
  return [h, s, l];
}

function hsl2rgb([h, s, l]) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

// Relative luminance (0..1) for picking on-accent text.
function luminance([r, g, b]) {
  const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

// ── Cover sampling ─────────────────────────────────────────────────────────────

// Average the cover's pixels weighted by saturation, so a vibrant accent emerges
// rather than a muddy mean. Returns [r,g,b] or null.
function extractColor(img) {
  const N = 32;
  const cv = document.createElement("canvas");
  cv.width = cv.height = N;
  const ctx = cv.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, N, N);
  let data;
  try { data = ctx.getImageData(0, 0, N, N).data; } catch { return null; }
  let r = 0, g = 0, b = 0, wsum = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 200) continue;                 // skip transparent
    const px = [data[i], data[i + 1], data[i + 2]];
    const [, s, l] = rgb2hsl(px);
    if (l < 0.08 || l > 0.95) continue;              // skip near black/white
    const w = s * s + 0.05;                           // favour saturated pixels
    r += px[0] * w; g += px[1] * w; b += px[2] * w; wsum += w;
  }
  if (!wsum) return null;
  return [r / wsum, g / wsum, b / wsum];
}

// ── Apply / clear ──────────────────────────────────────────────────────────────

const ACCENT_VARS = ["--accent", "--on-accent", "--bg", "--panel", "--line", "--muted", "--code-bg"];

// Surfaces recolored to the album hue, with a per-surface saturation cap and a
// fallback colour (used if the theme var can't be read).
const ART_SURFACES = [
  ["--bg",      0.40, [30, 30, 46]],
  ["--panel",   0.40, [39, 41, 61]],
  ["--line",    0.45, [58, 61, 82]],
  ["--code-bg", 0.40, [21, 22, 31]],
  ["--muted",   0.18, [154, 160, 181]],   // text — keep it readable, just hinted
];

function clearAccent() {
  const root = document.documentElement;
  for (const v of ACCENT_VARS) root.style.removeProperty(v);
  delete root.dataset.art;
}

// Turn a source rgb into [accentHex, onAccentHex], made vivid with a
// theme-appropriate lightness and a legible text colour over it.
function accentFor(rgb) {
  const dark = document.documentElement.dataset.theme !== "light";
  let [h, s] = rgb2hsl(rgb);
  s = clamp(s, 0.45, 0.95);
  const accent = hsl2rgb([h, s, dark ? 0.68 : 0.45]);
  const onAccent = luminance(accent) > 0.55 ? "#11131f" : "#ffffff";
  return [toHex(accent), onAccent];
}

// Recolor a base palette colour to `hue`, keeping its lightness; sat capped per surface.
function recolor(baseRgb, hue, sat) {
  const [, , l] = rgb2hsl(baseRgb);
  return toHex(hsl2rgb([hue, sat, l]));
}

// Album-art tint: set the accent AND recolor every surface to the art's hue,
// preserving each surface's theme lightness so text contrast is kept.
function applyArtAccent(rgb) {
  const root = document.documentElement;
  // Read the active theme's base palette with no inline overrides in the way.
  for (const v of ACCENT_VARS) root.style.removeProperty(v);
  const cs = getComputedStyle(root);
  const [hue, artSat] = rgb2hsl(rgb);

  const [accent, onAccent] = accentFor(rgb);
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--on-accent", onAccent);
  for (const [v, cap, fallback] of ART_SURFACES) {
    const base = parseColor(cs.getPropertyValue(v)) || fallback;
    root.style.setProperty(v, recolor(base, hue, clamp(artSat, 0, cap)));
  }
  root.dataset.art = "on";
}

// User-chosen fixed accent: set only --accent/--on-accent, no background wash.
function applyBaseAccent(rgb) {
  const root = document.documentElement;
  for (const v of ACCENT_VARS) root.style.removeProperty(v);
  delete root.dataset.art;
  const [accent, onAccent] = accentFor(rgb);
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--on-accent", onAccent);
}

// Fall back to the chosen accent, or fully revert to the theme default.
function applyBaseOrClear() {
  if (baseColor) applyBaseAccent(baseColor);
  else clearAccent();
}

// ── Derivation pipeline ──────────────────────────────────────────────────────

// Precedence: album-art accent (when enabled + cover available) → chosen base
// colour → theme default.
function derive() {
  if (!enabled) { applyBaseOrClear(); return; }
  const albumPath = getCurrentAlbumPath();
  if (!albumPath) { lastRGB = null; applyBaseOrClear(); return; }
  const img = new Image();
  img.onload = () => {
    const rgb = extractColor(img);
    if (rgb) { lastRGB = rgb; applyArtAccent(rgb); }
    else { lastRGB = null; applyBaseOrClear(); }
  };
  img.onerror = () => { lastRGB = null; applyBaseOrClear(); };
  img.src = "/api/cover?path=" + encodeURIComponent(albumPath);
}

// ── Public API ────────────────────────────────────────────────────────────────

export async function initAccent() {
  try {
    const s = await jget("/api/settings");
    enabled = !!s.theme_accent_from_art;
    baseColor = parseColor(s.theme_accent_color) || null;
  } catch { enabled = false; baseColor = null; }
  subscribe(() => derive());   // fires now and on every track change
}

export function setEnabled(on) {
  enabled = !!on;
  derive();
}

// Live-preview a chosen fixed accent ("" / invalid → theme default).
export function setBaseColor(hex) {
  baseColor = parseColor(hex) || null;
  derive();
}

// Recompute against the current theme (e.g. after a dark/light switch) without
// re-fetching the cover.
export function refresh() {
  if (enabled && lastRGB) applyArtAccent(lastRGB);
  else applyBaseOrClear();
}
