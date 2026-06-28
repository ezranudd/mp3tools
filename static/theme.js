// Light/dark theme, persisted in localStorage. Applied via <html data-theme>.
// The initial value is also set by an inline <head> script to avoid a flash.
const KEY = "mp3tools-theme";

export function getTheme() {
  return localStorage.getItem(KEY) === "light" ? "light" : "dark";
}

export function applyTheme(t) {
  document.documentElement.dataset.theme = t === "light" ? "light" : "dark";
}

export function setTheme(t) {
  t = t === "light" ? "light" : "dark";
  localStorage.setItem(KEY, t);
  applyTheme(t);
}

export function toggleTheme() {
  setTheme(getTheme() === "dark" ? "light" : "dark");
  return getTheme();
}

// Mobile: follow the device's system light/dark instead of a manual toggle. Applies
// the current preference and re-applies on change. `onChange` lets the caller hook
// in (e.g. refresh the album-art accent). The matching head script handles the
// pre-paint value, so there's no flash.
export function initSystemTheme(onChange = null) {
  const mq = matchMedia("(prefers-color-scheme: light)");
  applyTheme(mq.matches ? "light" : "dark");
  // Only fire onChange on subsequent changes — the initial apply runs before the
  // caller has set up the accent, which it initializes separately right after.
  mq.addEventListener("change", () => {
    applyTheme(mq.matches ? "light" : "dark");
    if (onChange) onChange();
  });
}
