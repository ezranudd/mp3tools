// Import view: drag-and-drop folders into the browser (uploaded to a temp dir on
// the server) OR point at a server-side path; preview + prompts via the global tracker.
import { startJob, mountJobPane, setPreviewRenderer } from "./jobs.js";
import { toast, escapeHtml, escapeAttr, openModal, closeModal, enableRowDrag } from "./util.js";

const CONFIDENT_SCORE = 140;                 // mirrors fetch_art.CONFIDENT_MATCH_SCORE
const BITRATES = [128, 160, 192, 256, 320];
const REQUIRED_ALBUM = ["album", "albumartist", "year"];

const AUDIO_EXTS = [".mp3", ".flac", ".m4a", ".alac"];
const IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"];
const KEEP_EXTS = new Set([...AUDIO_EXTS, ...IMAGE_EXTS]);

const extOf = (name) => {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
};
const wanted = (name) => KEEP_EXTS.has(extOf(name));
const isAudio = (name) => AUDIO_EXTS.includes(extOf(name));

let dropped = [];   // [{ file, path }] collected from a drop / folder pick
let pageEl = null;  // the mounted Import view, for the inline preview renderer

export function show(el) {
  pageEl = el;
  setPreviewRenderer(renderImportPreview);
  el.innerHTML = `<div class="page">
    <h2>Import</h2>
    <p class="muted">Drop music folders below (or use a server path). You'll review and
      edit every album graphically — tags, cover art, and lossless bitrate — before
      anything is copied.</p>

    <div id="dropZone" class="dropzone">
      <div class="dzbig">Drop folders here</div>
      <div class="muted">or click to choose a folder</div>
      <div id="dropSummary" class="muted" style="margin-top:8px"></div>
      <input type="file" id="dirPicker" webkitdirectory directory multiple hidden>
    </div>

    <div class="row" style="justify-content:flex-start;margin-top:12px">
      <button class="btn primary" id="runBtn" disabled>Start import</button>
      <span id="upStatus" class="muted"></span>
    </div>

    <details style="margin-top:14px">
      <summary class="muted">Or import from a folder on the server</summary>
      <div class="field" style="margin-top:8px">
        <label style="min-width:auto">Source folder</label>
        <input id="srcPath" placeholder="/path/to/incoming/music" style="width:360px">
      </div>
      <div class="row" style="justify-content:flex-start">
        <button class="btn" id="runPathBtn">Start import from path</button>
      </div>
    </details>

    <div id="importPreview"></div>
    <div id="jobArea" style="margin-top:14px"></div>
  </div>`;

  const zone = el.querySelector("#dropZone");
  const picker = el.querySelector("#dirPicker");
  const runBtn = el.querySelector("#runBtn");
  const summary = el.querySelector("#dropSummary");
  const upStatus = el.querySelector("#upStatus");
  dropped = [];

  const refresh = () => {
    if (!dropped.length) { summary.textContent = ""; runBtn.disabled = true; return; }
    const folders = new Set(dropped.map(d => d.path.split("/")[0]));
    const audio = dropped.filter(d => isAudio(d.file.name)).length;
    const images = dropped.length - audio;
    summary.textContent =
      `${folders.size} folder${folders.size === 1 ? "" : "s"} · ` +
      `${audio} audio · ${images} image${images === 1 ? "" : "s"}`;
    runBtn.disabled = audio === 0;
    if (audio === 0) summary.textContent += " — no audio files found";
  };

  zone.onclick = () => picker.click();
  zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
  zone.ondragleave = () => zone.classList.remove("over");
  zone.ondrop = async (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    summary.textContent = "Reading…";
    dropped = await collectFromDrop(e.dataTransfer);
    refresh();
  };
  picker.onchange = () => {
    dropped = [...picker.files]
      .filter(f => wanted(f.name))
      .map(f => ({ file: f, path: f.webkitRelativePath || f.name }));
    refresh();
  };

  runBtn.onclick = async () => {
    runBtn.disabled = true;
    try {
      const token = await uploadAll(dropped, (n, m) => {
        upStatus.textContent = `Uploading ${n}/${m}…`;
      });
      upStatus.textContent = "";
      await startJob("import", { upload_token: token });
    } catch (e) {
      upStatus.textContent = "";
      toast(e.message, true);
      runBtn.disabled = false;
    }
  };

  el.querySelector("#runPathBtn").onclick = async () => {
    const source = el.querySelector("#srcPath").value.trim();
    if (!source) { toast("Enter a source folder.", true); return; }
    try { await startJob("import", { source }); }
    catch (e) { toast(e.message, true); }
  };

  mountJobPane(el.querySelector("#jobArea"), { kind: "import" });
}

// Upload each collected file into a fresh server-side session; returns its token.
async function uploadAll(files, onProgress) {
  const { token } = await (await fetch("/api/import/upload/start", { method: "POST" })).json();
  for (let i = 0; i < files.length; i++) {
    const { file, path } = files[i];
    const r = await fetch("/api/import/upload/file?token=" + encodeURIComponent(token) +
                          "&path=" + encodeURIComponent(path), {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    onProgress(i + 1, files.length);
  }
  return token;
}

// ── Drag-drop folder traversal (webkitGetAsEntry) ─────────────────────────────

async function collectFromDrop(dt) {
  const entries = [...dt.items]
    .map(it => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean);
  const acc = [];
  for (const entry of entries) await walkEntry(entry, "", acc);
  return acc;
}

async function walkEntry(entry, prefix, acc) {
  if (entry.isFile) {
    if (!wanted(entry.name)) return;
    const file = await new Promise((res, rej) => entry.file(res, rej));
    acc.push({ file, path: prefix + entry.name });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    for (const child of await readAllEntries(reader)) {
      await walkEntry(child, prefix + entry.name + "/", acc);
    }
  }
}

// readEntries returns at most ~100 entries per call; keep reading until empty.
function readAllEntries(reader) {
  return new Promise((resolve, reject) => {
    const out = [];
    const step = () => reader.readEntries(batch => {
      if (!batch.length) return resolve(out);
      out.push(...batch);
      step();
    }, reject);
    step();
  });
}

// ── Inline import preview (edit-menu-style album sections) ────────────────────

// Registered with jobs.js: render the preview prompt inline in the Import view.
// Returns {proceed, entries} | {proceed:false}, or null to fall back to the modal
// (e.g. when the Import view isn't currently mounted). Fully graphical: tags, cover
// art (auto-searched in the background), and lossless bitrate are all edited here.
function renderImportPreview(p) {
  const host = pageEl && pageEl.isConnected ? pageEl.querySelector("#importPreview") : null;
  if (!host) return null;

  // Merge entries that will land in the same library album (album-artist + album +
  // year — matching the import's destination), so multi-disc folders show as one
  // section. Order each album's tracks smartly: disc, then track#, then source path.
  const groups = new Map();
  for (const e of p.entries) {
    const key = [(e.albumartist || e.artist), e.album, e.year]
      .map(s => (s || "").trim().toLowerCase()).join("|");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const albums = [...groups.values()].map((rows, idx) => {
    rows.sort((a, b) => discOf(a) - discOf(b) || trackOf(a) - trackOf(b)
                        || a.src.localeCompare(b.src));
    return ({
    idx, folder: rows[0].src.slice(0, rows[0].src.lastIndexOf("/")), rows,
    hasLossless: rows.some(r => r.lossless),
    hasConflict: rows.some(r => r.conflict),
    bitrate: p.default_bitrate || 320,
    conflict: "add",
    // art.mode: "source" (keep folder cover) | "url" (chosen/found) | "none" (placeholder)
    art: { mode: "source", url: null, results: null, state: "init" },
    });
  });

  host.innerHTML = `<div class="importpreview">
    <h3>Review import — ${albums.length} album${albums.length === 1 ? "" : "s"}</h3>
    <p class="muted">Edit tags, cover art and bitrate. Track order sets the numbering.</p>
    ${albums.map(renderSection).join("")}
    <div class="previewactions">
      <button class="btn" data-cancel>Cancel</button>
      <button class="btn primary" data-ok>Import</button>
    </div>
  </div>`;
  host.scrollIntoView({ block: "start" });

  for (const album of albums) wireSection(host, album);
  const okBtn = host.querySelector("[data-ok]");
  const validate = () => { okBtn.disabled = !albums.every(a => albumValid(host, a)); };
  host.querySelectorAll(".importpreview input").forEach(i => i.addEventListener("input", validate));
  validate();

  return new Promise(resolve => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; host.innerHTML = ""; resolve(v); } };
    okBtn.onclick = () => finish({ proceed: true, entries: collectEntries(host, albums) });
    host.querySelector("[data-cancel]").onclick = () => finish({ proceed: false });
  });
}

// Disc/track numbers for the smart initial order. Disc from TPOS, else the source
// folder name (…CD2/Disc 2…); track from the leading integer of TRCK.
function discOf(e) {
  const t = parseInt(String(e.disc || "").split("/")[0], 10);
  if (!isNaN(t)) return t;
  const m = /(?:cd|disc)\s*0*(\d+)/i.exec(e.src || "");
  return m ? parseInt(m[1], 10) : 1;
}
function trackOf(e) {
  const t = parseInt(String(e.track || "").split("/")[0], 10);
  return isNaN(t) ? 9999 : t;
}

function renderSection(album) {
  const f = album.rows[0];
  const trackRows = album.rows.map((e, n) => `
    <tr data-i="${e.i}">
      <td><span class="draghandle" title="Drag to reorder">⠿</span> <span class="num">${n + 1}</span></td>
      <td><input class="tag" data-f="title" value="${escapeAttr(e.title)}"></td>
      <td><input class="tag" data-f="artist" value="${escapeAttr(e.artist)}"></td>
    </tr>`).join("");
  const bitrate = album.hasLossless ? `
    <label class="muted" style="display:flex;align-items:center;gap:6px">Bitrate
      <select data-bitrate>${BITRATES.map(b =>
        `<option value="${b}" ${b === album.bitrate ? "selected" : ""}>${b} kbps</option>`).join("")}</select>
    </label>` : "";
  const conflict = album.hasConflict ? `
    <label class="warn" style="display:flex;align-items:center;gap:6px" title="This album already exists in the library">
      Already in library
      <select data-conflict><option value="add">Add to it</option><option value="skip">Skip</option></select>
    </label>` : "";
  return `<section class="albumsection" data-idx="${album.idx}">
    <div class="albumhead">
      <div class="importcover" data-cover title="Choose cover art"></div>
      <div class="albummeta">
        <input class="hdr title" data-a="album" value="${escapeAttr(f.album)}" placeholder="Album title">
        <div class="sub">
          <input class="hdr sub" data-a="albumartist" value="${escapeAttr(f.albumartist || f.artist)}" placeholder="Album artist"> ·
          <input class="hdr sub" data-a="year" value="${escapeAttr(f.year)}" placeholder="Year"> ·
          <input class="hdr sub" data-a="genre" value="${escapeAttr(f.genre || "")}" placeholder="Genre">
        </div>
        <div class="sub" style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
          <span>${album.rows.length} track${album.rows.length === 1 ? "" : "s"}</span>
          ${bitrate}${conflict}
        </div>
      </div>
    </div>
    <table>
      <thead><tr><th>#</th><th>Title</th><th>Artist</th></tr></thead>
      <tbody>${trackRows}</tbody>
    </table>
  </section>`;
}

// Size an inline header field to its content (mirrors tree.js so the
// "Album artist · Year · Genre" line stays tight, like the edit menu).
let _measureEl = null;
function autosizeField(inp) {
  if (!_measureEl) {
    _measureEl = document.createElement("span");
    _measureEl.style.cssText =
      "position:absolute;left:-9999px;top:-9999px;visibility:hidden;white-space:pre;";
    document.body.appendChild(_measureEl);
  }
  const cs = getComputedStyle(inp);
  _measureEl.style.fontSize = cs.fontSize;
  _measureEl.style.fontFamily = cs.fontFamily;
  _measureEl.style.fontWeight = cs.fontWeight;
  _measureEl.style.fontStyle = cs.fontStyle;
  _measureEl.style.letterSpacing = cs.letterSpacing;
  _measureEl.textContent = inp.value || inp.placeholder || "";
  inp.style.width = (_measureEl.offsetWidth + 1) + "px";
}

function wireSection(host, album) {
  const sec = host.querySelector(`.albumsection[data-idx="${album.idx}"]`);
  album.coverEl = sec.querySelector("[data-cover]");
  album.coverEl.onclick = () => openArtPicker(album);

  // Hug the sub-line fields to their content (album artist / year / genre).
  sec.querySelectorAll(".albummeta input.hdr.sub").forEach(inp => {
    autosizeField(inp);
    inp.addEventListener("input", () => autosizeField(inp));
  });

  // Drag tracks to reorder; renumber the visible # after each drop. Submit order
  // (DOM order) is what import uses for the final track numbers.
  const tbody = sec.querySelector("tbody");
  enableRowDrag(tbody, () => {
    tbody.querySelectorAll("tr .num").forEach((s, n) => s.textContent = n + 1);
  });
  const br = sec.querySelector("[data-bitrate]");
  if (br) br.onchange = () => { album.bitrate = parseInt(br.value, 10); };
  const cf = sec.querySelector("[data-conflict]");
  if (cf) cf.onchange = () => { album.conflict = cf.value; };

  // Start with the source folder cover; if there isn't one, search online.
  const img = new Image();
  img.onload = () => { album.art = { mode: "source", state: "found" }; paintCover(album); };
  img.onerror = () => { searchAlbumArt(album); };
  album.srcCoverUrl = "/api/import/cover?path=" + encodeURIComponent(album.folder);
  img.src = album.srcCoverUrl;
}

function albumArtistOf(album) {
  const sec = album.coverEl.closest(".albumsection");
  return sec.querySelector('[data-a="albumartist"]').value.trim();
}
function albumTitleOf(album) {
  const sec = album.coverEl.closest(".albumsection");
  return sec.querySelector('[data-a="album"]').value.trim();
}

async function searchAlbumArt(album) {
  const artist = albumArtistOf(album), title = albumTitleOf(album);
  if (!artist && !title) { album.art = { mode: "none", state: "done" }; paintCover(album); return; }
  album.art = { mode: "none", state: "searching" };
  paintCover(album);
  try {
    const r = await fetch(`/api/art/search?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(title)}`);
    const data = await r.json();
    const results = (data.results || []).filter(x => x.url);
    const top = results[0];
    if (top && (top.score || 0) >= CONFIDENT_SCORE) {
      album.art = { mode: "url", url: top.url, results, state: "done" };
    } else {
      album.art = { mode: "none", results, state: "done" };
    }
  } catch (e) {
    album.art = { mode: "none", results: [], state: "done" };
  }
  paintCover(album);
}

function paintCover(album) {
  const el = album.coverEl;
  const a = album.art;
  if (a.state === "searching") {
    el.className = "importcover shimmer";
    el.innerHTML = `<span class="covertext">Searching…</span>`;
  } else if (a.mode === "url" && a.url) {
    el.className = "importcover";
    el.innerHTML = `<img class="cover" src="${escapeAttr(a.url)}" alt="">`;
  } else if (a.mode === "source") {
    el.className = "importcover";
    el.innerHTML = `<img class="cover" src="${escapeAttr(album.srcCoverUrl)}" alt="">`;
  } else {
    el.className = "importcover placeholder";
    el.innerHTML = `<span class="covertext">No art<br><small>click to choose</small></span>`;
  }
}

async function openArtPicker(album) {
  // Lazily search if we haven't yet (e.g. a source cover was present).
  if (!album.art.results) {
    const artist = albumArtistOf(album), title = albumTitleOf(album);
    try {
      const r = await fetch(`/api/art/search?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(title)}`);
      album.art.results = ((await r.json()).results || []).filter(x => x.url);
    } catch (e) { album.art.results = []; }
  }
  const results = album.art.results || [];
  const tiles = results.map((rr, i) => `
    <div class="art" data-pick="${i}"><img src="${escapeAttr(rr.url)}" loading="lazy">
      <div class="cap">${escapeHtml(rr.source_label || rr.source || "")} · ${rr.score ?? ""}</div></div>`).join("");
  openModal(`
    <h3>Cover art — ${escapeHtml(albumTitleOf(album) || "album")}</h3>
    <div class="grid">
      ${album.srcCoverUrl ? `<div class="art" data-src><img src="${escapeAttr(album.srcCoverUrl)}" onerror="this.closest('.art').style.display='none'"><div class="cap">Folder cover</div></div>` : ""}
      <div class="art" data-none><div class="coverph" style="aspect-ratio:1;display:flex;align-items:center;justify-content:center">No art</div><div class="cap">Placeholder</div></div>
      ${tiles || `<p class="muted">No online results.</p>`}
    </div>
    <div class="row"><button class="btn" data-close>Close</button></div>`,
    (box) => {
      box.querySelectorAll("[data-pick]").forEach(t => t.onclick = () => {
        const rr = results[+t.dataset.pick];
        album.art = { mode: "url", url: rr.url, results, state: "done" };
        paintCover(album); closeModal();
      });
      const src = box.querySelector("[data-src]");
      if (src) src.onclick = () => { album.art = { mode: "source", results, state: "found" }; paintCover(album); closeModal(); };
      box.querySelector("[data-none]").onclick = () => { album.art = { mode: "none", results, state: "done" }; paintCover(album); closeModal(); };
      box.querySelector("[data-close]").onclick = () => closeModal();
    });
}

function albumValid(host, album) {
  const sec = host.querySelector(`.albumsection[data-idx="${album.idx}"]`);
  let ok = true;
  REQUIRED_ALBUM.forEach(a => {
    const inp = sec.querySelector(`[data-a="${a}"]`);
    const empty = !inp.value.trim();
    inp.classList.toggle("needs", empty);
    if (empty) ok = false;
  });
  sec.querySelectorAll('input[data-f="title"]').forEach(inp => {
    const empty = !inp.value.trim();
    inp.classList.toggle("needs", empty);
    if (empty) ok = false;
  });
  return ok;
}

function collectEntries(host, albums) {
  const out = [];
  for (const album of albums) {
    const sec = host.querySelector(`.albumsection[data-idx="${album.idx}"]`);
    const alb = {};
    sec.querySelectorAll(".albummeta input[data-a]").forEach(i => alb[i.dataset.a] = i.value);
    const art = album.art.mode === "url" ? { art_url: album.art.url }
              : album.art.mode === "none" ? { art_none: true } : {};
    sec.querySelectorAll("tr[data-i]").forEach(tr => {
      const row = {
        i: Number(tr.dataset.i),
        album: alb.album, albumartist: alb.albumartist, year: alb.year, genre: alb.genre,
        conflict: album.hasConflict ? album.conflict : undefined,
        ...(album.hasLossless ? { bitrate: album.bitrate } : {}),
        ...art,
      };
      tr.querySelectorAll("input[data-f]").forEach(inp => row[inp.dataset.f] = inp.value);
      out.push(row);
    });
  }
  return out;
}
