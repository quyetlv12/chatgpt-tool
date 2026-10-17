const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
assert.match(html, /id="close-tool-windows"[^>]*\bhidden\b/);

function element() {
  return {
    hidden: true, disabled: false, textContent: 'Đóng tất cả tab Chrome',
    handlers: {}, dataset: { module: 'browser' },
    classList: { toggle() {}, remove() {} },
    addEventListener(name, callback) { this.handlers[name] = callback; },
  };
}

async function main() {
  const elements = Object.fromEntries(
    ['workspace', 'loading', 'open-module', 'close-tool-windows'].map(id => [id, element()]),
  );
  const button = elements['close-tool-windows'];
  let status = { modules: [{ key: 'browser', ready: true }], browser_windows_open: false };
  let statusOK = true;
  let closeOK = true;
  let pendingStatus;
  let interval;
  const context = vm.createContext({
    document: {
      querySelectorAll: () => [element()],
      getElementById: id => elements[id],
    },
    window: { setTimeout: callback => callback() },
    setInterval: (callback, delay) => { interval = callback; assert.equal(delay, 3000); },
    fetch: async url => {
      if (url === '/api/windows/close') {
        if (closeOK) status.browser_windows_open = false;
        return { ok: closeOK, json: async () => ({ closed: closeOK ? 1 : 0 }) };
      }
      if (pendingStatus) return pendingStatus;
      return { ok: statusOK, json: async () => status };
    },
  });
  vm.runInContext(source, context);
  await context.refreshStatus();
  assert.equal(button.hidden, true, 'ready modules without windows must not show button');

  status.browser_windows_open = true;
  await interval();
  assert.equal(button.hidden, false, 'open window shows button');
  status.browser_windows_open = false;
  await interval();
  assert.equal(button.hidden, true, 'manual close hides button');

  status.browser_windows_open = true;
  await interval();
  await button.handlers.click();
  assert.equal(button.hidden, true, 'close-all refreshes immediately');
  assert.equal(button.disabled, false);

  status.browser_windows_open = true;
  closeOK = false;
  await interval();
  await button.handlers.click();
  assert.equal(button.hidden, false, 'failed close preserves open-window action');
  assert.equal(button.disabled, false);

  delete status.browser_windows_open;
  await interval();
  assert.equal(button.hidden, true, 'missing availability fails closed');
  status.browser_windows_open = true;
  statusOK = false;
  await interval();
  assert.equal(button.hidden, true, 'HTTP error fails closed');
  statusOK = true;

  let resolveOld;
  pendingStatus = new Promise(resolve => { resolveOld = resolve; });
  const oldRefresh = context.refreshStatus();
  pendingStatus = null;
  status.browser_windows_open = false;
  await context.refreshStatus();
  resolveOld({ ok: true, json: async () => ({ modules: [], browser_windows_open: true }) });
  await oldRefresh;
  assert.equal(button.hidden, true, 'late old response cannot reshow button');
  console.log('Suite UI window visibility checks passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
