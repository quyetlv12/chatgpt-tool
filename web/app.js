const buttons = [...document.querySelectorAll("nav button[data-module]")];
const frame = document.getElementById("workspace");
const loading = document.getElementById("loading");
const openModule = document.getElementById("open-module");
const closeToolWindows = document.getElementById("close-tool-windows");

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
    if (!response.ok) throw new Error(result.error || "Không thể đóng cửa sổ Chrome");
    const count = Number(result.closed || 0);
    closeToolWindows.textContent = count > 0 ? `Đã đóng ${count} nhóm cửa sổ` : "Không có tab tool đang mở";
    window.setTimeout(() => { closeToolWindows.textContent = label; }, 2200);
  } catch (error) {
    closeToolWindows.textContent = "Đóng tab Chrome · lỗi";
    window.setTimeout(() => { closeToolWindows.textContent = label; }, 2200);
  } finally {
    closeToolWindows.disabled = false;
  }
});

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const { modules } = await response.json();
    const states = new Map(modules.map((module) => [module.key, module.ready]));
    buttons.forEach((button) => {
      button.classList.toggle("ready", Boolean(states.get(button.dataset.module)));
    });
  } catch (_error) {
    buttons.forEach((button) => button.classList.remove("ready"));
  }
}

refreshStatus();
setInterval(refreshStatus, 10000);
