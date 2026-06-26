// Audit view: read-only compliance scan rendered as a grouped report.
import { jget, escapeHtml } from "./util.js";

let container;

export function show(el) {
  container = el;
  el.innerHTML = `<div class="page">
    <h2>Audit</h2>
    <p class="muted">Read-only compliance scan of the library.</p>
    <div style="margin:10px 0"><button class="btn primary" id="runAudit">Run audit</button></div>
    <div id="auditOut"></div>
  </div>`;
  el.querySelector("#runAudit").onclick = run;
}

async function run() {
  const out = container.querySelector("#auditOut");
  out.innerHTML = `<p class="muted">Scanning…</p>`;
  try {
    const data = await jget("/api/audit");
    render(out, data);
  } catch (e) {
    out.innerHTML = `<p class="err">${escapeHtml(e.message)}</p>`;
  }
}

function issuesHtml(issues) {
  return issues.map(i =>
    `<div><span class="pill">${escapeHtml(i.label)}</span>${escapeHtml(i.msg)}</div>`).join("");
}

function render(out, data) {
  const t = data.totals;
  const problem = data.albums.filter(a => a.album_issues.length || a.files.some(f => f.issues.length));
  let html = `<p>${t.albums_with_issues} of ${t.albums} albums have issues
    · ${t.files_with_issues} of ${t.files} files flagged.</p>`;
  if (!problem.length) {
    html += `<p class="ok">All compliant. 🎉</p>`;
    out.innerHTML = html;
    return;
  }
  for (const a of problem) {
    const fileIssues = a.files.filter(f => f.issues.length);
    html += `<div class="card">
      <h4>${escapeHtml(a.name)}</h4>
      ${a.album_issues.length ? issuesHtml(a.album_issues) : ""}
      ${fileIssues.map(f => `<div style="margin-top:6px">
          <div class="muted">${escapeHtml(f.name)}</div>${issuesHtml(f.issues)}
        </div>`).join("")}
    </div>`;
  }
  out.innerHTML = html;
}
