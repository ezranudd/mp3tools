// Structural edits: rename/merge/year/genre/album-artist moves, artist edits, art removal.
// All go through /api/edit/* (build_edit + apply_edits) or /api/art/remove.
import { jpost, toast, promptModal } from "./util.js";

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
