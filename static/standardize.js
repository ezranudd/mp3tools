// Standardize view: launch a standardize job; prompts/progress via the global tracker.
import { startJob, mountJobPane, disableWhileBusy } from "./jobs.js";
import { toast } from "./util.js";

export function show(el) {
  el.innerHTML = `<div class="page">
    <h2>Standardize</h2>
    <p class="muted">Runs the full standardization pipeline over the library, using
      your saved Settings (cover art, preserve flags, online art fetch).
      Interactive steps (fill missing tags, confirm deletions, pick lossless
      bitrate) prompt in the browser.</p>
    <div class="field" style="margin-top:10px">
      <label style="min-width:auto"><input type="checkbox" id="dryRun" checked>
        Dry run (preview only — no files changed)</label>
    </div>
    <div class="row" style="justify-content:flex-start">
      <button class="btn primary" id="runBtn">Run standardize</button>
    </div>
    <div id="jobArea" style="margin-top:14px"></div>
  </div>`;

  const runBtn = el.querySelector("#runBtn");
  runBtn.onclick = async () => {
    const dry_run = el.querySelector("#dryRun").checked;
    try { await startJob("standardize", { dry_run }); }
    catch (e) { toast(e.message, true); }
  };
  disableWhileBusy(runBtn);   // can't standardize while another operation runs
  // Step bar by default; the full change log is tucked behind "Show details".
  mountJobPane(el.querySelector("#jobArea"), { kind: "standardize", collapsible: true });
}
