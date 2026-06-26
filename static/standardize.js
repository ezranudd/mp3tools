// Standardize view: launch a standardize job; prompts surface via jobs.js.
import { runJob } from "./jobs.js";

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
  runBtn.onclick = () => {
    const dry_run = el.querySelector("#dryRun").checked;
    runBtn.disabled = true;
    runJob("standardize", { dry_run }, el.querySelector("#jobArea"),
      { onDone: () => { runBtn.disabled = false; } });
  };
}
