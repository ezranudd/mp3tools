// Shared helpers: fetch wrappers, escaping, toast, modal.

export async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

export async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

export function escapeAttr(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

let _toastTimer;
export function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => (t.className = "toast"), 2400);
}

const _modal = () => document.getElementById("modal");
const _modalBox = () => document.getElementById("modalBox");

// Render arbitrary HTML into the shared modal. `setup(box, close)` wires events.
export function openModal(html, setup) {
  const box = _modalBox();
  box.innerHTML = html;
  _modal().classList.add("show");
  if (setup) setup(box, closeModal);
}

export function closeModal() {
  _modal().classList.remove("show");
  _modalBox().innerHTML = "";
}

// A small text/choice prompt helper returning a Promise.
// kind="text": resolves to string|null. kind="choice": resolves to option key|null.
export function promptModal({ title, kind = "text", value = "", options = [] }) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; closeModal(); resolve(v); } };
    let body;
    if (kind === "choice") {
      body = `<div class="row" style="flex-wrap:wrap;justify-content:flex-start">` +
        options.map(o => `<button class="btn" data-k="${escapeAttr(o.key)}">${escapeHtml(o.label)}</button>`).join("") +
        `</div>`;
    } else {
      body = `<input id="pmInput" style="width:100%" value="${escapeAttr(value)}">`;
    }
    openModal(
      `<h3>${escapeHtml(title)}</h3>${body}` +
      (kind === "text" ? `<div class="row"><button class="btn" data-cancel>Cancel</button>
        <button class="btn primary" data-ok>OK</button></div>` : ""),
      (box) => {
        box.querySelectorAll("[data-k]").forEach(b =>
          b.onclick = () => finish(b.dataset.k));
        const input = box.querySelector("#pmInput");
        if (input) {
          input.focus(); input.select();
          input.onkeydown = e => {
            if (e.key === "Enter") finish(input.value);
            if (e.key === "Escape") finish(null);
          };
        }
        const ok = box.querySelector("[data-ok]");
        if (ok) ok.onclick = () => finish(input.value);
        const cancel = box.querySelector("[data-cancel]");
        if (cancel) cancel.onclick = () => finish(null);
      });
  });
}
