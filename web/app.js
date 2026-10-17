const buttons = [...document.querySelectorAll("nav button[data-module]")];
const frame = document.getElementById("workspace");
const loading = document.getElementById("loading");
const openModule = document.getElementById("open-module");
const closeToolWindows = document.getElementById("close-tool-windows");
let statusVersion = 0;
let statusPending = 0;

function select(button) {
  buttons.forEach((item) => item.classList.toggle("active", item === button));
  frame.src = button.dataset.url;
  openModule.href = button.dataset.url;
  loading.hidden = false;
}

buttons.forEach((button) => button.addEventListener("click", () => select(button)));
frame.addEventListener("load", () => { loading.hidden = true; });

closeToolWindows.addEventListener("click", async () => {
  closeToolWindows.disabled = true;
  const label = closeToolWindows.textContent;
  closeToolWindows.textContent = "Đang đóng…";
  try {
    const response = await fetch("/api/windows/close", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Không thể đóng cửa sổ trình duyệt");
    const count = Number(result.closed || 0);
    closeToolWindows.textContent = count > 0 ? `Đã đóng ${count} nhóm cửa sổ` : "Không có tab tool đang mở";
    window.setTimeout(() => { closeToolWindows.textContent = label; }, 2200);
  } catch (error) {
    closeToolWindows.textContent = "Đóng tab trình duyệt · lỗi";
    window.setTimeout(() => { closeToolWindows.textContent = label; }, 2200);
  } finally {
    closeToolWindows.disabled = false;
    await refreshStatus();
  }
});

async function refreshStatus() {
  const version = ++statusVersion;
  statusPending += 1;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Không thể đọc trạng thái tool");
    const { modules, browser_windows_open } = await response.json();
    if (version !== statusVersion) return;
    const states = new Map(modules.map((module) => [module.key, module.ready]));
    closeToolWindows.hidden = browser_windows_open !== true;
    buttons.forEach((button) => {
      button.classList.toggle("ready", Boolean(states.get(button.dataset.module)));
    });
  } catch (_error) {
    if (version !== statusVersion) return;
    closeToolWindows.hidden = true;
    buttons.forEach((button) => button.classList.remove("ready"));
  } finally {
    statusPending -= 1;
  }
}

refreshStatus();
setInterval(() => { if (!statusPending) return refreshStatus(); }, 3000);
