// Generic job runner: start a job, stream its log/progress, service prompts.
// Prompt kinds: "text" / "choice" (via promptModal) and "preview" (import table).
import { jget, jpost, toast, escapeHtml, escapeAttr, promptModal, openModal, closeModal } from "./util.js";

// Render the job runner into `container` and drive it to completion.
export async function runJob(kind, params, container, { onDone } = {}) {
  let jid;
  try {
    const r = await jpost("/api/jobs", { kind, ...params });
    jid = r.job_id;
  } catch (e) { toast(e.message, true); return; }

  container.innerHTML = `
    <div class="field"><strong id="jobProg" class="warn"></strong></div>
    <div class="log" id="jobLog"></div>
    <div class="row" style="justify-content:flex-start;margin-top:8px">
      <button class="btn danger" id="jobCancel">Cancel</button>
    </div>`;
  const logEl = container.querySelector("#jobLog");
  const progEl = container.querySelector("#jobProg");
  const cancelBtn = container.querySelector("#jobCancel");
  cancelBtn.onclick = () => jpost(`/api/jobs/${jid}/cancel`, {}).catch(() => {});

  let answering = false;
  async function tick() {
    let j;
    try { j = await jget(`/api/jobs/${jid}`); }
    catch { return setTimeout(tick, 800); }

    progEl.textContent = j.progress || "";
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    logEl.textContent = j.log.join("\n");
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;

    if (j.state === "waiting" && !answering) {
      answering = true;
      const value = await answerPrompt(j.prompt);
      try { await jpost(`/api/jobs/${jid}/respond`, { value }); } catch (e) { toast(e.message, true); }
      answering = false;
      return setTimeout(tick, 40);
    }
    if (j.state === "done" || j.state === "error") {
      cancelBtn.style.display = "none";
      progEl.textContent = j.state === "error" ? "Error" : "Finished";
      progEl.className = j.state === "error" ? "err" : "ok";
      if (onDone) onDone(j);
      return;
    }
    setTimeout(tick, 500);
  }
  tick();
}

async function answerPrompt(p) {
  if (p.kind === "preview") return await previewModal(p);
  if (p.kind === "choice") {
    const k = await promptModal({ title: p.prompt, kind: "choice", options: p.options });
    return k ?? "";
  }
  const v = await promptModal({ title: p.prompt.trim() || "Enter value", kind: "text" });
  return v ?? "";
}

// Editable import preview: render rows, allow per-field edits, return {proceed, entries}.
function previewModal(p) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; closeModal(); resolve(v); } };
    const rows = p.entries.map(e => `
      <tr data-i="${e.i}">
        <td class="muted">${escapeHtml(e.name)}</td>
        <td><input data-f="track" value="${escapeAttr(e.track)}" style="width:60px"></td>
        <td><input data-f="title" value="${escapeAttr(e.title)}" style="width:100%"></td>
        <td><input data-f="artist" value="${escapeAttr(e.artist)}" style="width:100%"></td>
        <td><input data-f="album" value="${escapeAttr(e.album)}" style="width:100%"></td>
        <td><input data-f="year" value="${escapeAttr(e.year)}" style="width:60px"></td>
      </tr>`).join("");
    openModal(`
      <h3>Import preview — ${p.entries.length} track${p.entries.length === 1 ? "" : "s"}
        ${p.has_lossless ? `<span class="pill">lossless present</span>` : ""}</h3>
      <p class="muted">Edit any field before importing. Lossless bitrate is chosen via the next prompt.</p>
      <div style="max-height:55vh;overflow:auto">
      <table><thead><tr><th>File</th><th>#</th><th>Title</th><th>Artist</th><th>Album</th><th>Year</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <div class="row">
        <button class="btn" data-cancel>Cancel import</button>
        <button class="btn primary" data-ok>Import</button>
      </div>`,
      (box) => {
        const collect = () => [...box.querySelectorAll("tr[data-i]")].map(tr => {
          const row = { i: Number(tr.dataset.i) };
          tr.querySelectorAll("input[data-f]").forEach(inp => row[inp.dataset.f] = inp.value);
          return row;
        });
        box.querySelector("[data-ok]").onclick = () => finish({ proceed: true, entries: collect() });
        box.querySelector("[data-cancel]").onclick = () => finish({ proceed: false });
      });
  });
}
