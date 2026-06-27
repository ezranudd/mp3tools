// Import view: drag-and-drop folders into the browser (uploaded to a temp dir on
// the server) OR point at a server-side path; preview + prompts via the global tracker.
import { startJob, mountJobPane } from "./jobs.js";
import { toast, escapeHtml } from "./util.js";

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

export function show(el) {
  el.innerHTML = `<div class="page">
    <h2>Import</h2>
    <p class="muted">Drop music folders below (or use a server path). Tracks are copied
      into the library with normalized tags; lossless files are converted. You'll
      review a preview and answer any prompts in the browser.</p>

    <div id="dropZone" class="dropzone">
      <div class="dzbig">Drop folders here</div>
      <div class="muted">or click to choose a folder</div>
      <div id="dropSummary" class="muted" style="margin-top:8px"></div>
      <input type="file" id="dirPicker" webkitdirectory directory multiple hidden>
    </div>

    <div class="field" style="margin-top:12px">
      <label style="min-width:auto"><input type="checkbox" id="dryRun">
        Dry run (preview only — nothing copied; files are still uploaded to scan)</label>
    </div>
    <div class="row" style="justify-content:flex-start">
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
    const dry_run = el.querySelector("#dryRun").checked;
    runBtn.disabled = true;
    try {
      const token = await uploadAll(dropped, (n, m) => {
        upStatus.textContent = `Uploading ${n}/${m}…`;
      });
      upStatus.textContent = "";
      await startJob("import", { upload_token: token, dry_run });
    } catch (e) {
      upStatus.textContent = "";
      toast(e.message, true);
      runBtn.disabled = false;
    }
  };

  el.querySelector("#runPathBtn").onclick = async () => {
    const source = el.querySelector("#srcPath").value.trim();
    if (!source) { toast("Enter a source folder.", true); return; }
    const dry_run = el.querySelector("#dryRun").checked;
    try { await startJob("import", { source, dry_run }); }
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
