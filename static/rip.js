// Import-from-CD view: rip the disc in the host's optical drive to FLAC, then run
// the ripped tracks through the same graphical import preview as a normal import.
// The heavy lifting (rip → import hand-off) is a server-side "rip" job; this view
// just picks the drive, starts the job, and shows its log/progress.
import { startJob, mountJobPane, setPreviewRenderer, disableWhileBusy, isBusy } from "./jobs.js";
import { renderImportPreview } from "./import.js";
import { jget, toast, escapeHtml, escapeAttr } from "./util.js";

export async function show(el) {
  // Reuse the Import view's rich preview (tags + cover + lossless bitrate). The
  // #importPreview div below is its host; it must be present and connected so the
  // bitrate gets set — lossless tracks with no bitrate are dropped on import.
  setPreviewRenderer(renderImportPreview);
  el.innerHTML = `<div class="page">
    <h2>Import from CD</h2>
    <p class="muted">Rip the disc in this machine's optical drive to FLAC, then review and
      edit every track graphically before it's imported. Requires <code>cdparanoia</code>
      and <code>ffmpeg</code> installed on the server.</p>

    <div class="field" style="margin-top:8px">
      <label>Drive</label>
      <select id="ripDevice"><option>Scanning…</option></select>
      <button class="btn" id="refreshBtn" title="Re-scan drives">↻</button>
    </div>

    <div class="row" style="justify-content:flex-start;margin-top:12px">
      <button class="btn primary" id="ripBtn" disabled>Rip &amp; import</button>
    </div>

    <div id="importPreview"></div>
    <div id="jobArea" style="margin-top:14px"></div>
  </div>`;

  const sel = el.querySelector("#ripDevice");
  const ripBtn = el.querySelector("#ripBtn");
  const refreshBtn = el.querySelector("#refreshBtn");

  async function loadDevices() {
    sel.innerHTML = `<option>Scanning…</option>`;
    let devices = [];
    try { devices = (await jget("/api/rip/devices")).devices || []; }
    catch (e) { toast(e.message, true); }
    if (!devices.length) {
      sel.innerHTML = `<option value="">No optical drive detected</option>`;
      ripBtn.disabled = true;
      return;
    }
    sel.innerHTML = devices
      .map(d => `<option value="${escapeAttr(d)}">${escapeHtml(d)}</option>`).join("");
    ripBtn.disabled = isBusy();
  }
  await loadDevices();
  refreshBtn.onclick = loadDevices;

  ripBtn.onclick = async () => {
    const device = sel.value;
    if (!device) { toast("No drive selected.", true); return; }
    try { await startJob("rip", { device }); }
    catch (e) { toast(e.message, true); }
  };

  // Block ripping while any job is active or no drive is selected.
  disableWhileBusy(ripBtn, () => !sel.value);
  mountJobPane(el.querySelector("#jobArea"), { kind: "rip", log: true });
}
