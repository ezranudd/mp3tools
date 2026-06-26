// Browse / Edit mode state, persisted in localStorage. Default: browse.
const KEY = "mp3tools-mode";
const subs = new Set();
let mode = localStorage.getItem(KEY) === "edit" ? "edit" : "browse";

export function getMode() { return mode; }
export function isEdit() { return mode === "edit"; }

export function setMode(m) {
  m = m === "edit" ? "edit" : "browse";
  if (m === mode) return;
  mode = m;
  localStorage.setItem(KEY, m);
  subs.forEach(fn => fn(m));
}

export function onModeChange(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}
