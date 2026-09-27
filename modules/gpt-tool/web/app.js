const FORMATS = [
  ["cockpit", "Cockpit"],
  ["9router", "9router"],
  ["sub2api", "sub2api"],
];

const FORMAT_CONFIRMATION = {
  sub2api: "Xuất JSON sub2api",
  cockpit: "Tự nhập vào Cockpit",
  "9router": "Tự thêm vào 9Router",
};

const STEP_LABEL = {
  queued: "đang chờ worker",
  parse: "đọc dòng",
  login: "đang login",
  oauth: "đang OAuth Codex",
  refresh: "đang refresh token",
  export: "đang ghi JSON",
  done: "xong",
  cancelled: "đã dừng",
};

const STORE_KEY = "gpt-tool-ui";

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

function savePrefs() {
  const fmt = selectedFormat();
  localStorage.setItem(
    STORE_KEY,
    JSON.stringify({
      format: fmt,
      proxy: document.getElementById("proxy").value,
      workers: clampWorkers(document.getElementById("workers").value),
    })
  );
}

function clampWorkers(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 2;
  return Math.max(1, Math.min(8, Math.round(n)));
}

const formatsEl = document.getElementById("formats");
FORMATS.forEach(([id, label]) => {
  const el = document.createElement("label");
  el.innerHTML = `<input type="radio" name="fmt" value="${id}"> ${label}`;
  formatsEl.appendChild(el);
});

const prefs = loadPrefs();
const defaultFormat = FORMATS.some(([id]) => id === prefs.format) ? prefs.format : "cockpit";
document.querySelector(`input[name="fmt"][value="${defaultFormat}"]`).checked = true;
if (prefs.proxy) document.getElementById("proxy").value = prefs.proxy;
document.getElementById("workers").value = clampWorkers(prefs.workers ?? document.getElementById("workers").value);
updateFormatConfirmation();
document.getElementById("formats").addEventListener("change", () => {
  updateFormatConfirmation();
  savePrefs();
});
document.getElementById("proxy").addEventListener("change", savePrefs);
document.getElementById("workers").addEventListener("change", () => {
  document.getElementById("workers").value = clampWorkers(document.getElementById("workers").value);
  savePrefs();
});

function selectedFormat() {
  const el = document.querySelector('input[name="fmt"]:checked');
  return el ? el.value : "";
}

function updateFormatConfirmation() {
  document.getElementById("format-confirmation-text").textContent = FORMAT_CONFIRMATION[selectedFormat()] || "";
}

const rows = new Map();
let currentJobId = null;

function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.hidden = !text;
  el.className = "log-banner " + (cls || "");
  el.textContent = text || "";
}

function jobLines(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter((raw) => raw && !raw.startsWith("#"));
}

function seedRows(text) {
  rows.clear();
  jobLines(text).forEach((line, index) => {
    const email = line.split("|")[0].trim().toLowerCase();
    if (!email) return;
    rows.set(String(index), { email, state: "queued", step: "queued", index });
  });
}

function applyJob(running, results) {
  const finished = new Set();
  (results || []).forEach((item, i) => {
    const key = item.index != null ? String(item.index) : String(i);
    finished.add(key);
    rows.set(key, {
      email: item.email,
      state: item.cancelled ? "cancelled" : item.ok ? "ok" : "fail",
      step: item.ok ? "done" : item.step || "fail",
      path: item.path,
      error: item.error,
      copyable: !!item.copyable,
      cockpitSent: !!item.cockpit_sent,
      nineRouterSent: !!item.nine_router_sent,
      nineRouterError: item.nine_router_error,
      index: item.index != null ? item.index : i,
    });
  });
  const live = new Set();
  Object.entries(running || {}).forEach(([key, val]) => {
    const rec = val && typeof val === "object" ? val : { email: key, step: val };
    const id = rec.index != null ? String(rec.index) : String(key);
    if (finished.has(id)) return;
    live.add(id);
    const prev = rows.get(id) || { email: rec.email || key };
    rows.set(id, {
      ...prev,
      email: rec.email || prev.email,
      state: "run",
      step: rec.step || val,
    });
  });
  for (const [id, row] of rows) {
    if (finished.has(id) || live.has(id) || row.state !== "run") continue;
    rows.set(id, { ...row, state: "queued", step: "queued" });
  }
}

function renderLog(total, busy) {
  const list = document.getElementById("line-log");
  const items = [...rows.values()];
  const ok = items.filter((row) => row.state === "ok").length;
  const fail = items.filter((row) => row.state === "fail").length;
  const cancelled = items.filter((row) => row.state === "cancelled").length;
  const done = ok + fail + cancelled;
  const all = total || items.length;
  document.getElementById("log-spinner").hidden = !busy;
  const progress = document.getElementById("log-progress");
  const bar = document.getElementById("log-bar");
  progress.hidden = !all;
  bar.style.width = all ? `${Math.round((done / all) * 100)}%` : "0%";
  const summary = document.getElementById("live-summary");
  const running = items.filter((row) => row.state === "run").length;
  const slots = clampWorkers(document.getElementById("workers").value);
  const stopped = cancelled ? ` · ${cancelled} đã dừng` : "";
  if (busy) summary.textContent = `${done}/${all} · ${running}/${slots} worker · ${ok} OK · ${fail} lỗi${stopped}`;
  else if (all) summary.textContent = `Xong ${done}/${all} · ${ok} OK · ${fail} lỗi${stopped}`;
  else summary.textContent = "Chưa chạy.";
  const copyAll = document.getElementById("copy-all");
  copyAll.hidden = busy || !currentJobId || ok === 0;
  list.innerHTML = items
    .map((row) => {
      const pill = STEP_LABEL[row.step] || row.step || "";
      const detail = row.state === "ok"
        ? ["JSON sẵn sàng", row.cockpitSent && "Đã gửi Cockpit", row.nineRouterSent && "Đã thêm 9Router", row.nineRouterError].filter(Boolean).join(" · ")
        : row.error || "";
      const copyButton = row.state === "ok" && row.copyable
        ? `<button type="button" class="copy-json" data-index="${row.index}" aria-label="Copy JSON ${escapeHtml(row.email)}">Copy JSON</button>`
        : "";
      return `<li class="log-row ${row.state}">
        <span class="dot"></span>
        <div>
          <div class="email">${escapeHtml(row.email)}</div>
          ${detail ? `<div class="meta">${escapeHtml(detail)}</div>` : ""}
        </div>
        <div class="row-actions">${copyButton}<span class="pill">${escapeHtml(pill)}</span></div>
      </li>`;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const fallback = document.createElement("textarea");
  fallback.value = text;
  fallback.setAttribute("readonly", "");
  fallback.style.position = "fixed";
  fallback.style.opacity = "0";
  document.body.appendChild(fallback);
  fallback.select();
  const copied = document.execCommand("copy");
  fallback.remove();
  if (!copied) throw new Error("Clipboard unavailable");
}

async function copyJobJson(target, button) {
  if (!currentJobId) return;
  const original = button.textContent;
  button.disabled = true;
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(currentJobId)}/copy/${encodeURIComponent(target)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || "Không thể đọc JSON");
    await writeClipboard(JSON.stringify(body.data, null, 2));
    button.textContent = "Đã copy";
    setStatus(target === "all" ? `Đã copy ${body.data.length} account vào clipboard.` : "Đã copy JSON vào clipboard.", "ok");
    setTimeout(() => { button.textContent = original; }, 2200);
  } catch (error) {
    setStatus(error.message || "Không thể copy JSON", "err");
  } finally {
    button.disabled = false;
  }
}

function renderResults(results, total, busy) {
  applyJob({}, results);
  renderLog(total || results.length, !!busy);
}

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
    document.getElementById("tab-export").classList.toggle("hidden", tab !== "export");
    document.getElementById("tab-convert").classList.toggle("hidden", tab !== "convert");
  });
});

const runExport = document.getElementById("run-export");
const RUN_BUTTON_HTML = runExport.innerHTML;
const STOP_BUTTON_HTML = '<svg class="button-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="6" y="6" width="12" height="12" rx="1"></rect></svg>Dừng tiến trình';

function setRunButton(busy, stopping = false) {
  runExport.classList.toggle("primary", !busy);
  runExport.classList.toggle("stop-job", busy);
  runExport.dataset.action = busy ? "stop" : "run";
  runExport.disabled = stopping || (busy && !currentJobId);
  runExport.innerHTML = busy ? (stopping ? "Đang dừng..." : STOP_BUTTON_HTML) : RUN_BUTTON_HTML;
}

setRunButton(false);

document.getElementById("run-export").addEventListener("click", async () => {
  if (runExport.dataset.action === "stop") {
    await stopCurrentJob();
    return;
  }
  const format = selectedFormat();
  if (!format) {
    setStatus("Chọn định dạng ở bước 1 trước khi chạy.", "err");
    return;
  }
  savePrefs();
  currentJobId = null;
  setRunButton(true);
  seedRows(document.getElementById("lines").value);
  renderLog(rows.size, true);
  setStatus("");
  const res = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      format,
      lines: document.getElementById("lines").value,
      proxy: document.getElementById("proxy").value,
      workers: clampWorkers(document.getElementById("workers").value),
      cockpit_import: format === "cockpit",
      nine_router_import: format === "9router",
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    setRunButton(false);
    renderLog(rows.size, false);
    setStatus(data.error || "export lỗi", "err");
    return;
  }
  const id = data.id;
  currentJobId = id;
  setRunButton(true);
  renderLog(rows.size, true);
  const poll = async () => {
    const job = await (await fetch("/api/jobs/" + id)).json();
    const results = job.results || [];
    const total = job.total || rows.size;
    applyJob(job.running || {}, results);
    renderLog(total, !job.done);
    if (!job.done) {
      setTimeout(poll, 400);
      return;
    }
    setRunButton(false);
    const integrationErrors = [job.cockpit?.error, job.nine_router?.error].filter(Boolean);
    const integrationSuccess = [
      job.cockpit?.sent && `Cockpit: ${job.cockpit.count}`,
      job.nine_router?.sent && `9Router: ${job.nine_router.count}`,
    ].filter(Boolean);
    if (job.cancelled) setStatus("Đã dừng các tiến trình.", "ok");
    else if (job.error) setStatus(job.error, "err");
    else if (integrationErrors.length) setStatus(integrationErrors.join(" · "), "err");
    else if (integrationSuccess.length) setStatus(`Đã nhập tự động — ${integrationSuccess.join(" · ")}.`, "ok");
  };
  poll();
});

document.getElementById("run-convert").addEventListener("click", async () => {
  const format = selectedFormat();
  if (!format) {
    setStatus("Chọn định dạng ở bước 1 trước khi chạy.", "err");
    return;
  }
  rows.clear();
  currentJobId = null;
  renderLog(0, true);
  setStatus("");
  const res = await fetch("/api/convert", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format, text: document.getElementById("json-in").value }),
  });
  const data = await res.json();
  if (!res.ok) {
    renderLog(0, false);
    setStatus(data.error || "convert lỗi", "err");
    return;
  }
  currentJobId = data.id;
  rows.clear();
  renderResults(data.results || [], (data.results || []).length, false);
});

document.getElementById("open-out").addEventListener("click", async () => {
  await fetch("/api/open-out", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
});

document.getElementById("shutdown-tool").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const res = await fetch("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!res.ok) throw new Error("Không thể tắt tool");
    button.textContent = "Đã tắt";
    document.querySelector(".workspace-status").textContent = "stopped";
    setStatus("Tool đã tắt. Bạn có thể đóng tab.", "ok");
  } catch (error) {
    button.disabled = false;
    setStatus(error.message || "Không thể tắt tool", "err");
  }
});

async function stopCurrentJob() {
  if (!currentJobId) return;
  setRunButton(true, true);
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(currentJobId)}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) throw new Error("Không thể dừng tiến trình");
    setStatus("Đang dừng các tiến trình...", "");
  } catch (error) {
    setRunButton(true);
    setStatus(error.message || "Không thể dừng tiến trình", "err");
  }
}

document.getElementById("line-log").addEventListener("click", (event) => {
  const button = event.target.closest(".copy-json");
  if (!button) return;
  copyJobJson(button.dataset.index, button);
});

document.getElementById("copy-all").addEventListener("click", (event) => {
  copyJobJson("all", event.currentTarget);
});
