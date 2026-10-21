const form = document.getElementById("backup-form");
const startButton = document.getElementById("start");
const stopButton = document.getElementById("stop");
const formError = document.getElementById("form-error");
const selectionCard = document.getElementById("selection-card");
const backupSelected = document.getElementById("backup-selected");
const selectionError = document.getElementById("selection-error");
let currentState = {};
let catalogRunId = "";
let loadingRunId = "";
let conversationInputs = [];
let selectionPending = false;
let statusVersion = 0;
let lastRunning = false;

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Yêu cầu thất bại");
  return body;
}

function renderStatus(state) {
  currentState = state;
  const total = Number(state.total || 0);
  const completed = Number(state.done || 0) + Number(state.failed || 0) + Number(state.skipped || 0);
  document.getElementById("status-message").textContent = state.message || "Sẵn sàng";
  document.getElementById("stage").textContent = state.stage || "idle";
  document.getElementById("done").textContent = Number(state.done || 0);
  document.getElementById("failed").textContent = Number(state.failed || 0);
  document.getElementById("skipped").textContent = Number(state.skipped || 0);
  document.getElementById("total").textContent = total;
  document.getElementById("progress-bar").style.width = `${total ? Math.min(100, completed / total * 100) : 0}%`;
  document.getElementById("accounts-label").textContent = state.source && state.target ? `${state.source}  →  ${state.target}` : "";
  startButton.disabled = state.running === true;
  stopButton.disabled = state.running !== true;
  if (catalogRunId && catalogRunId !== state.run_id) {
    catalogRunId = "";
    conversationInputs = [];
    document.getElementById("conversation-list").replaceChildren();
    selectionCard.hidden = true;
  }
  updateSelection();
  if (state.running && state.stage === "choosing" && catalogRunId !== state.run_id) {
    loadConversations(state.run_id);
  }
  if (lastRunning && !state.running) loadHistory();
  lastRunning = state.running === true;
}

async function refreshStatus() {
  const version = statusVersion;
  try {
    const state = await jsonFetch("/api/status");
    if (version === statusVersion) renderStatus(state);
  }
  catch (_error) {
    if (version === statusVersion) document.getElementById("status-message").textContent = "Mất kết nối với module";
  }
}

function updateSelection() {
  const enabled = currentState.running && currentState.stage === "choosing"
    && catalogRunId === currentState.run_id && !selectionPending;
  const selected = conversationInputs.filter(input => input.checked).length;
  document.getElementById("selection-count").textContent = `Đã chọn ${selected} / ${conversationInputs.length}`;
  for (const input of conversationInputs) input.disabled = !enabled;
  document.getElementById("select-all").disabled = !enabled;
  document.getElementById("select-none").disabled = !enabled;
  backupSelected.disabled = !enabled || selected === 0;
}

async function loadConversations(runId) {
  if (loadingRunId === runId) return;
  loadingRunId = runId;
  try {
    const catalog = await jsonFetch("/api/conversations");
    if (currentState.run_id !== runId || currentState.stage !== "choosing" || catalog.run_id !== runId) return;
    conversationInputs = [];
    const rows = catalog.items.map(item => {
      const row = document.createElement("label");
      row.className = "conversation-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = item.id;
      input.checked = false;
      input.addEventListener("change", updateSelection);
      const title = document.createElement("span");
      title.textContent = item.title || item.id;
      row.append(input, title);
      conversationInputs.push(input);
      return row;
    });
    document.getElementById("conversation-list").replaceChildren(...rows);
    catalogRunId = runId;
    selectionError.textContent = "";
    selectionCard.hidden = false;
    updateSelection();
  } catch (error) {
    formError.textContent = error.message;
  } finally {
    if (loadingRunId === runId) loadingRunId = "";
  }
}

for (const [id, checked] of [["select-all", true], ["select-none", false]]) {
  document.getElementById(id).addEventListener("click", () => {
    for (const input of conversationInputs) input.checked = checked;
    updateSelection();
  });
}

backupSelected.addEventListener("click", async () => {
  if (backupSelected.disabled) return;
  const payload = {
    run_id: catalogRunId,
    conversation_ids: conversationInputs.filter(input => input.checked).map(input => input.value),
  };
  selectionPending = true;
  statusVersion += 1;
  updateSelection();
  selectionError.textContent = "";
  try {
    renderStatus(await jsonFetch("/api/jobs/selection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
  } catch (error) {
    selectionError.textContent = error.message;
  } finally {
    statusVersion += 1;
    selectionPending = false;
    updateSelection();
  }
});

function historyRow(item) {
  const row = document.createElement("tr");
  const title = document.createElement("td");
  title.textContent = item.title || item.conversation_id;
  title.title = item.error || "";
  const status = document.createElement("td");
  status.textContent = item.status;
  const date = document.createElement("td");
  date.textContent = item.updated_at ? new Date(item.updated_at).toLocaleString("vi-VN") : "";
  row.append(title, status, date);
  return row;
}

async function loadHistory() {
  const body = document.getElementById("history");
  try {
    const { items } = await jsonFetch("/api/history");
    body.replaceChildren(...(items.length ? items.map(historyRow) : []));
    if (!items.length) body.innerHTML = '<tr><td colspan="3" class="empty">Chưa có dữ liệu</td></tr>';
  } catch (_error) {
    body.innerHTML = '<tr><td colspan="3" class="empty">Không đọc được checkpoint</td></tr>';
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  formError.textContent = "";
  startButton.disabled = true;
  statusVersion += 1;
  const payload = {
    source: document.getElementById("source").value,
    target: document.getElementById("target").value,
    marker_message: document.getElementById("marker").value,
    limit: Number(document.getElementById("limit").value || 0),
    cleanup_links: document.getElementById("cleanup").checked,
  };
  try {
    const state = await jsonFetch("/api/jobs/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("source").value = "";
    document.getElementById("target").value = "";
    renderStatus(state);
  } catch (error) {
    formError.textContent = error.message;
    startButton.disabled = false;
  } finally {
    statusVersion += 1;
  }
});

stopButton.addEventListener("click", async () => {
  stopButton.disabled = true;
  statusVersion += 1;
  try { renderStatus(await jsonFetch("/api/jobs/stop", { method: "POST" })); }
  catch (error) { formError.textContent = error.message; }
  finally { statusVersion += 1; }
});

document.getElementById("refresh-history").addEventListener("click", loadHistory);
refreshStatus();
loadHistory();
setInterval(refreshStatus, 1500);
