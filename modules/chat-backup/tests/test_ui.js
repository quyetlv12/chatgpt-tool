const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
assert.doesNotMatch(source, /localStorage|sessionStorage/, 'credentials must never be stored in browser storage');
assert.match(html, /id="source" type="text"[^>]*autocomplete="off"/);
assert.match(html, /id="target" type="text"[^>]*autocomplete="off"/);

function element(value = '') {
  return {
    value, checked: false, disabled: false, hidden: true, textContent: '', innerHTML: '', title: '',
    style: {}, handlers: {}, children: [],
    addEventListener(name, callback) { this.handlers[name] = callback; },
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
  };
}

async function main() {
  const ids = [
    'backup-form', 'start', 'stop', 'form-error', 'source', 'target', 'marker', 'limit',
    'cleanup', 'status-message', 'stage', 'done', 'failed', 'skipped', 'total',
    'progress-bar', 'accounts-label', 'history', 'refresh-history',
    'selection-card', 'backup-selected', 'selection-error', 'selection-count',
    'select-all', 'select-none', 'conversation-list',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  elements.source.value = 'source@example.com|secret|JBSWY3DPEHPK3PXP';
  elements.target.value = 'target@example.com|secret|JBSWY3DPEHPK3PXP';
  elements.marker.value = 'Đã lưu';
  elements.limit.value = '3';
  elements.cleanup.checked = true;
  let submitted;
  let selectedPayload;
  let catalogRequests = 0;
  let rejectSelection = false;
  let captureStaleStatus = false;
  let holdStatus = false;
  let releaseStatus;
  let staleRefresh;
  const idle = { run_id: '', running: false, stage: 'idle', message: 'Sẵn sàng', total: 0 };
  let state = idle;
  const items = [
    { id: 'c1', title: 'Chat một' },
    { id: 'c2', title: '<img src=x onerror=alert(1)>' },
    { id: 'c3', title: '' },
  ];
  const context = vm.createContext({
    document: {
      getElementById: id => elements[id],
      createElement: () => element(),
    },
    fetch: async (url, options = {}) => {
      assert.notEqual(url, '/api/jobs/start', 'UI must scan before any copying');
      if (url === '/api/jobs/scan') {
        submitted = JSON.parse(options.body);
        state = { ...idle, run_id: 'run1', running: true, stage: 'starting' };
        return { ok: true, json: async () => state };
      }
      if (url === '/api/conversations') {
        catalogRequests += 1;
        return { ok: true, json: async () => ({ run_id: state.run_id, items }) };
      }
      if (url === '/api/jobs/selection') {
        selectedPayload = JSON.parse(options.body);
        assert.equal(elements['backup-selected'].disabled, true, 'double click must be blocked while submitting');
        if (rejectSelection) return { ok: false, json: async () => ({ error: 'Phiên chọn đã hết hạn' }) };
        if (captureStaleStatus) {
          holdStatus = true;
          staleRefresh = context.refreshStatus();
          holdStatus = false;
        }
        state = { ...state, stage: 'selected', total: 1 };
        return { ok: true, json: async () => state };
      }
      if (url === '/api/jobs/stop') {
        state = { ...state, running: false, stage: 'stopped' };
        return { ok: true, json: async () => state };
      }
      if (url === '/api/history') return { ok: true, json: async () => ({ items: [] }) };
      if (url === '/api/status' && holdStatus) {
        const snapshot = { ...state };
        return new Promise(resolve => { releaseStatus = () => resolve({ ok: true, json: async () => snapshot }); });
      }
      return { ok: true, json: async () => state };
    },
    setInterval() {},
    Date,
    console,
  });
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  await elements['backup-form'].handlers.submit({ preventDefault() {} });
  assert.equal(submitted.source, 'source@example.com|secret|JBSWY3DPEHPK3PXP');
  assert.equal(submitted.target, 'target@example.com|secret|JBSWY3DPEHPK3PXP');
  assert.equal(submitted.limit, 3);
  assert.equal(submitted.cleanup_links, true);
  assert.equal(elements.source.value, '', 'source secret is cleared after accepted start');
  assert.equal(elements.target.value, '', 'target secret is cleared after accepted start');

  state = { ...state, stage: 'choosing', total: 3 };
  await context.refreshStatus();
  await new Promise(setImmediate);
  const rows = elements['conversation-list'].children;
  const inputs = rows.map(row => row.children[0]);
  assert.equal(rows.length, 3);
  assert.equal(elements['selection-card'].hidden, false);
  assert.equal(elements['backup-selected'].disabled, true, 'no chat selected by default');
  assert.equal(elements['selection-count'].textContent, 'Đã chọn 0 / 3');
  assert.equal(rows[1].children[1].textContent, items[1].title, 'titles are text, never HTML');
  assert.equal(rows[1].children[1].innerHTML, '');
  assert.equal(rows[2].children[1].textContent, 'c3', 'missing title falls back to ID');

  inputs[1].checked = true;
  inputs[1].handlers.change();
  assert.equal(elements['backup-selected'].disabled, false);
  await context.refreshStatus();
  await new Promise(setImmediate);
  assert.equal(catalogRequests, 1, 'status polling must not reload catalog or clear selection');
  assert.equal(elements['conversation-list'].children, rows);
  assert.equal(inputs[1].checked, true);
  elements['select-all'].handlers.click();
  assert.ok(inputs.every(input => input.checked));
  assert.equal(elements['selection-count'].textContent, 'Đã chọn 3 / 3');
  elements['select-none'].handlers.click();
  assert.ok(inputs.every(input => !input.checked));
  assert.equal(elements['backup-selected'].disabled, true);
  await elements['backup-selected'].handlers.click();
  assert.equal(selectedPayload, undefined, 'zero selection must not submit');

  inputs[1].checked = true;
  inputs[1].handlers.change();
  captureStaleStatus = true;
  await elements['backup-selected'].handlers.click();
  releaseStatus();
  await staleRefresh;
  assert.deepEqual(selectedPayload, { run_id: 'run1', conversation_ids: ['c2'] });
  assert.ok(inputs.every(input => input.disabled), 'selection locks once backup starts');
  assert.equal(elements['select-all'].disabled, true);
  assert.equal(elements['backup-selected'].disabled, true);
  assert.equal(elements.stage.textContent, 'selected', 'late status polling must not restore an old choosing state');

  state = { ...state, run_id: 'run2', stage: 'choosing', total: 3 };
  await context.refreshStatus();
  await new Promise(setImmediate);
  const newInputs = elements['conversation-list'].children.map(row => row.children[0]);
  assert.ok(newInputs.every(input => !input.checked), 'new runs must not inherit old selections');
  assert.equal(catalogRequests, 2);
  newInputs[0].checked = true;
  newInputs[0].handlers.change();
  rejectSelection = true;
  await elements['backup-selected'].handlers.click();
  assert.equal(elements['selection-error'].textContent, 'Phiên chọn đã hết hạn');
  assert.equal(elements['backup-selected'].disabled, false, 'failed submission can be retried');
  assert.equal(newInputs[0].checked, true, 'failed submission keeps the selection');

  await elements.stop.handlers.click();
  assert.ok(newInputs.every(input => input.disabled));
  assert.equal(elements['backup-selected'].disabled, true);

  // A reloaded page in the choosing stage must fetch its catalog too.
  vm.runInContext('catalogRunId = ""; conversationInputs = [];', context);
  state = { ...state, running: true, stage: 'choosing' };
  await context.refreshStatus();
  await new Promise(setImmediate);
  assert.equal(catalogRequests, 3);
  assert.equal(elements['conversation-list'].children.length, 3);
  context.renderStatus({ ...state, stage: 'copying', current: 3, total: 3, done: 1, failed: 0, skipped: 1 });
  assert.equal(elements['progress-bar'].style.width, `${2 / 3 * 100}%`, 'in-flight chat must not count as complete');
  context.renderStatus({ ...state, stage: 'complete', running: false, total: 3, done: 2, failed: 0, skipped: 1 });
  assert.equal(elements['progress-bar'].style.width, '100%');
  console.log('Chat Backup UI checks passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
