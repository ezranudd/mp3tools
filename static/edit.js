// Structural edits: rename/merge/year/genre/album-artist moves, artist edits, art removal.
// All go through /api/edit/* (build_edit + apply_edits) or /api/art/remove.
import { jget, jpost, toast, promptModal, openModal, closeModal,
         escapeHtml, escapeAttr } from "./util.js";

// Generic single-field edit: prompt for a value, preview, apply.
export async function runEdit(path, op, title, current = "", onDone) {
  const value = await promptModal({ title, kind: "text", value: current });
  if (value == null || value === current) return;
  try {
    // Preview first so the user gets a meaningful confirmation / merge warning.
    const pv = await jpost("/api/edit/preview", { path, op, value });
    if (!pv.ok) { toast("No change.", true); return; }
    const res = await jpost("/api/edit/apply", { path, op, value });
    if (res.ok && !res.error) toast(res.desc);
    else toast(res.error || "Edit failed", true);
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

export async function removeArt(path, onDone) {
  const mode = await promptModal({
    title: "Remove artwork from this album",
    kind: "choice",
    options: [
      { key: "folder", label: "Folder files" },
      { key: "embed", label: "Embedded tags" },
      { key: "both", label: "Both" },
    ],
  });
  if (!mode) return;
  try {
    const res = await jpost("/api/art/remove", { path, mode });
    toast(res.removed ? `Removed art (${res.removed}).` : "No art found.");
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

// Album-level: permanently delete the album folder (single confirmation).
export async function deleteAlbum(path, label, onDone) {
  const choice = await promptModal({
    title: `Delete album "${label}"? This permanently removes the folder and its files.`,
    kind: "choice",
    options: [{ key: "delete", label: "Delete" }, { key: "cancel", label: "Cancel" }],
  });
  if (choice !== "delete") return;
  try {
    await jpost("/api/album/delete", { path });
    toast("Album deleted.");
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

// Track-level: permanently delete one song file (single confirmation). Passes the
// server result to onDone so the caller can tell "album now empty" from a plain delete.
export async function deleteTrack(path, label, onDone) {
  const choice = await promptModal({
    title: `Delete "${label}"? This permanently removes the track file.`,
    kind: "choice",
    options: [{ key: "delete", label: "Delete" }, { key: "cancel", label: "Cancel" }],
  });
  if (choice !== "delete") return;
  try {
    const res = await jpost("/api/track/delete", { path });
    if (res.ok && !res.error) toast("Track deleted.");
    else toast(res.error || "Delete failed", true);
    if (onDone) onDone(res);
  } catch (e) { toast(e.message, true); }
}

// Artist-level: choose rename or genre, then edit.
export async function editArtist(artist, onDone) {
  const choice = await promptModal({
    title: `Edit artist "${artist.label}"`,
    kind: "choice",
    options: [
      { key: "artist_rename", label: "Rename" },
      { key: "artist_genre", label: "Set genre (all albums)" },
    ],
  });
  if (!choice) return;
  const title = choice === "artist_rename" ? "New album-artist name" : "Genre for all albums";
  await runEdit(artist.path, choice, title, choice === "artist_rename" ? artist.label : "", onDone);
}

// Genre-level (owner): merge every album of *fromGenre* into another genre. Target
// is chosen from the other existing genres, or typed as a new name.
export async function mergeGenre(fromGenre, genres, onDone) {
  const options = (genres || [])
    .map(g => g.genre)
    .filter(name => name.toLowerCase() !== fromGenre.toLowerCase())
    .map(name => ({ key: name, label: name }));
  options.push({ key: "__custom__", label: "Type a new name…" });
  const pick = await promptModal({
    title: `Merge "${fromGenre}" into…`,
    kind: "choice",
    options,
  });
  if (!pick) return;
  let target = pick;
  if (pick === "__custom__") {
    target = await promptModal({ title: `Merge "${fromGenre}" into:`, kind: "text", value: "" });
    if (target == null) return;
    target = target.trim();
  }
  if (!target || target.toLowerCase() === fromGenre.toLowerCase()) return;
  try {
    const res = await jpost("/api/genre/merge", { from_genre: fromGenre, to_genre: target });
    if (res.ok && !res.error) toast(`Merged ${res.merged} album${res.merged === 1 ? "" : "s"} into "${target}".`);
    else toast(res.error || "Merge failed", true);
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

// ── Collections (owner-only album groups) ─────────────────────────────────────

export async function createCollection(onDone) {
  const name = await promptModal({ title: "New collection name", kind: "text", value: "" });
  if (name == null || !name.trim()) return;
  try {
    await jpost("/api/collection/create", { name: name.trim() });
    toast(`Created "${name.trim()}".`);
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

export async function renameCollection(name, onDone) {
  const next = await promptModal({ title: `Rename "${name}" to:`, kind: "text", value: name });
  if (next == null || !next.trim() || next.trim() === name) return;
  try {
    await jpost("/api/collection/rename", { name, new_name: next.trim() });
    toast("Renamed.");
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

export async function deleteCollection(name, onDone) {
  const choice = await promptModal({
    title: `Delete collection "${name}"? This only removes the grouping, not any albums.`,
    kind: "choice",
    options: [{ key: "delete", label: "Delete" }, { key: "cancel", label: "Cancel" }],
  });
  if (choice !== "delete") return;
  try {
    await jpost("/api/collection/delete", { name });
    toast("Collection deleted.");
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

// From an album: pick an existing collection (or create one) and add this album.
export async function addAlbumToCollection(albumPath, onDone) {
  let collections;
  try { collections = (await jget("/api/collections")).collections || []; }
  catch (e) { toast(e.message, true); return; }
  const options = collections.map(c => ({ key: c.name, label: c.name }));
  options.push({ key: "__new__", label: "New collection…" });
  const pick = await promptModal({ title: "Add album to…", kind: "choice", options });
  if (!pick) return;
  let name = pick;
  if (pick === "__new__") {
    const typed = await promptModal({ title: "New collection name", kind: "text", value: "" });
    if (typed == null || !typed.trim()) return;
    name = typed.trim();
    try { await jpost("/api/collection/create", { name }); }
    catch (e) { toast(e.message, true); return; }
  }
  try {
    await jpost("/api/collection/add", { name, album_path: albumPath });
    toast(`Added to "${name}".`);
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

export async function removeAlbumFromCollection(name, albumPath, onDone) {
  try {
    await jpost("/api/collection/remove", { name, album_path: albumPath });
    toast("Removed from collection.");
    if (onDone) onDone();
  } catch (e) { toast(e.message, true); }
}

// From within a collection: a filterable checkbox picker over every library album
// not already in it. Checking albums and confirming adds each. *existingPaths* are
// the album paths already in the collection (excluded from the list).
export async function addAlbumsToCollection(name, existingPaths, onDone) {
  let albums;
  try { albums = (await jget("/api/albums")).albums || []; }
  catch (e) { toast(e.message, true); return; }
  const have = new Set(existingPaths || []);
  const candidates = albums.filter(a => !have.has(a.album_path));
  if (!candidates.length) { toast("Every album is already in this collection."); return; }

  const rows = candidates.map((a, i) => `
    <label class="collpickrow" data-label="${escapeAttr(((a.album || "") + " " + (a.artist || "")).toLowerCase())}">
      <input type="checkbox" data-i="${i}">
      <span class="cpalbum">${escapeHtml(a.album || "Untitled")}</span>
      <span class="cpartist muted">${escapeHtml(a.artist || "")}</span>
    </label>`).join("");
  openModal(
    `<h3>Add albums to "${escapeHtml(name)}"</h3>
     <input id="collFilter" placeholder="Filter…" style="width:100%;margin-bottom:8px">
     <div id="collPickList" class="collpicklist">${rows}</div>
     <div class="row"><button class="btn" data-cancel>Cancel</button>
       <button class="btn primary" data-ok>Add selected</button></div>`,
    (box) => {
      const filter = box.querySelector("#collFilter");
      const list = box.querySelector("#collPickList");
      filter.focus();
      filter.oninput = () => {
        const q = filter.value.trim().toLowerCase();
        list.querySelectorAll(".collpickrow").forEach(r =>
          r.style.display = (!q || r.dataset.label.includes(q)) ? "" : "none");
      };
      box.querySelector("[data-cancel]").onclick = () => closeModal();
      box.querySelector("[data-ok]").onclick = async () => {
        const chosen = [...list.querySelectorAll("input[type=checkbox]:checked")]
          .map(cb => candidates[Number(cb.dataset.i)].album_path);
        closeModal();
        if (!chosen.length) return;
        let added = 0;
        for (const path of chosen) {
          try { await jpost("/api/collection/add", { name, album_path: path }); added++; }
          catch (e) { toast(e.message, true); }
        }
        if (added) toast(`Added ${added} album${added === 1 ? "" : "s"} to "${name}".`);
        if (onDone) onDone();
      };
    });
}
