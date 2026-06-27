// Import view: launch an import job from a source directory; preview + prompts via the global tracker.
import { startJob, mountJobPane } from "./jobs.js";
import { toast } from "./util.js";

export function show(el) {
  el.innerHTML = `<div class="page">
    <h2>Import</h2>
    <p class="muted">Copy tracks from a source folder into the library, normalizing
      tags and (optionally) converting lossless files. You'll review a preview and
      answer any prompts in the browser.</p>
    <div class="field" style="margin-top:10px">
      <label style="min-width:auto">Source folder</label>
      <input id="srcPath" placeholder="/path/to/incoming/music" style="width:360px">
    </div>
    <div class="field">
      <label style="min-width:auto"><input type="checkbox" id="dryRun">
        Dry run (preview only — nothing copied)</label>
    </div>
    <div class="row" style="justify-content:flex-start">
      <button class="btn primary" id="runBtn">Start import</button>
    </div>
    <div id="jobArea" style="margin-top:14px"></div>
  </div>`;

  el.querySelector("#runBtn").onclick = async () => {
    const source = el.querySelector("#srcPath").value.trim();
    if (!source) { toast("Enter a source folder.", true); return; }
    const dry_run = el.querySelector("#dryRun").checked;
    try { await startJob("import", { source, dry_run }); }
    catch (e) { toast(e.message, true); }
  };
  mountJobPane(el.querySelector("#jobArea"), { kind: "import" });
}
