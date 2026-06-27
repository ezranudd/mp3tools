// Full-window background image (foobar-style), stored server-side and shared.
// The image bytes live at /api/background; the tunables (opacity/blur/fit) come
// from /api/settings. applyBackground() reflects a settings dict into the DOM.
import { jget } from "./util.js";

// Apply a settings dict (as returned by /api/settings) to the background layer.
export function applyBackground(cfg) {
  const root = document.documentElement;
  const img = document.getElementById("bgImage");
  if (!cfg || !cfg.background_present) {
    delete root.dataset.bg;
    img.style.backgroundImage = "";
    return;
  }
  root.dataset.bg = "on";
  root.style.setProperty("--bg-opacity", String(cfg.background_opacity ?? 0.4));
  root.style.setProperty("--bg-blur", `${cfg.background_blur ?? 0}px`);
  // "tile" isn't a real background-size; repeat the image at native size instead.
  if (cfg.background_fit === "tile") {
    root.style.setProperty("--bg-fit", "auto");
    img.style.backgroundRepeat = "repeat";
  } else {
    root.style.setProperty("--bg-fit", cfg.background_fit || "cover");
    img.style.backgroundRepeat = "no-repeat";
  }
  // Cache-bust with the version so a replaced image shows up immediately.
  img.style.backgroundImage = `url("/api/background?v=${cfg.background_version || 0}")`;
}

export async function uploadBackground(file) {
  const r = await fetch("/api/background", {
    method: "POST",
    headers: { "Content-Type": file.type || "image/jpeg" },
    body: file,
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

export async function clearBackground() {
  const r = await fetch("/api/background", { method: "DELETE" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

export async function initBackground() {
  try {
    applyBackground(await jget("/api/settings"));
  } catch (e) { /* leave the plain themed background in place */ }
}
