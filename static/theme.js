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
