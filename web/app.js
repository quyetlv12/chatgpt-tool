const buttons = [...document.querySelectorAll("nav button")];
const frame = document.getElementById("workspace");
const loading = document.getElementById("loading");
const openModule = document.getElementById("open-module");

function select(button) {
  buttons.forEach((item) => item.classList.toggle("active", item === button));
  frame.src = button.dataset.url;
  openModule.href = button.dataset.url;
  loading.hidden = false;
}

buttons.forEach((button) => button.addEventListener("click", () => select(button)));
frame.addEventListener("load", () => { loading.hidden = true; });

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
