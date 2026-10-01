const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', 'change 2fa community', 'static');
const dashboardHtml = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const html = fs.readFileSync(path.join(root, 'passkey.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'passkey-ui.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'dashboard.css'), 'utf8');

for (const id of [
  'passkey-workspace', 'passkey-combo-input',
  'launch-passkey-jobs', 'passkey-job-list', 'passkey-logs', 'close-passkey-windows',
]) assert.match(html, new RegExp(`id="${id}"`));
assert.match(dashboardHtml, /id="open-passkey-tool"[^>]*href="\/passkey"/);
assert.doesNotMatch(dashboardHtml, /id="passkey-workspace"/);
assert.match(html, /passkey-ui\.js\?v=1\.1\.2/);
assert.match(html, /dashboard\.css\?v=1\.5\.19/);
assert.match(html, /<main id="passkey-workspace"[^>]*class="passkey-workspace passkey-page"/);
assert.doesNotMatch(html, /<dialog id="passkey-workspace"/);
assert.match(app, /\/api\/passkey\/jobs/);
assert.match(app, /LAUNCH_PASSKEY/);
assert.doesNotMatch(app, /window\.open\('about:blank'/);
assert.match(app, /native_window: true/);
assert.match(app, /handoff_ready/);
assert.match(app, /Đã mở cửa sổ Chrome passkey/);
assert.match(app, /\/api\/passkey\/windows\/close/);
assert.doesNotMatch(app, /window\.confirm\(/);
assert.match(app, /state\.opened\.clear\(\)/);
assert.match(app, /waitingForAutomaticTab = state\.autoOpen\.size > 0/);
assert.match(app, /payload\.jobs\.forEach\(settleAutomaticHandoff\)/);
assert.match(app, /await openWorkspace\(\)/);
assert.doesNotMatch(app, /passkey-workspace'\)\.showModal/);
assert.match(css, /\.passkey-page\{[^}]*grid-template-rows:auto minmax\(420px,\.9fr\) minmax\(480px,1\.1fr\)/);
assert.match(css, /\.passkey-route-shell\{[^}]*1880px/);
assert.doesNotMatch(app, /credentials\.create|privateKey/);
console.log('passkey batch UI: dedicated route, large workspace, concurrent handoff windows and key privacy passed');

// Execute the real launch handler with a deferred API response. Verify multiple
// submissions and draft edits during the request, not just source patterns.
const vm = require('node:vm');
async function checkSubmission({ fail = false } = {}) {
  const nodes = new Map();
  const getNode = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', disabled: false, textContent: '' });
    return nodes.get(id);
  };
  const state = { jobs: new Map([['old', { id: 'old', status: 'success' }]]),
    autoOpen: new Set() };
  let release;
  let nextId = 0;
  const submitted = [];
  const context = { state, $: getNode, renderInputCount() {}, renderJobs() {}, toast() {},
    syncEventVisibility() {},
    settleAutomaticHandoff() {},
    api(_, options) {
      submitted.push(JSON.parse(options.body).lines);
      return new Promise((resolve, reject) => { release = () => fail
        ? reject(new Error('HTTP 409'))
        : resolve({ jobs: [{ id: `new-${++nextId}`, status: 'queued' }] }); });
    },
  };
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('  async function launchJobs()'), app.indexOf('  async function jobAction(')), context);
  const input = getNode('passkey-combo-input');
  input.value = 'first@example.com|synthetic-password|TOTP';
  const pending = context.launchJobs();
  release();
  if (fail) await assert.rejects(pending, /HTTP 409/); else await pending;
  assert.equal(getNode('launch-passkey-jobs').disabled, false);
  assert.equal(state.jobs.get('old').status, 'success');
  if (fail) {
    assert.equal(input.value, submitted[0][0]);
    assert.equal(state.jobs.size, 1);
  } else {
    assert.equal(input.value, 'first@example.com|synthetic-password|TOTP');
    assert.ok(state.autoOpen.has('new-1'));
    input.value = 'second@example.com|synthetic-password|TOTP';
    const second = context.launchJobs(); release(); await second;
    assert.equal(state.jobs.size, 3);
    assert.ok(state.autoOpen.has('new-2'));
  }
}
async function checkNativeHandoff(opened, invalid = false) {
  const state = { jobs: new Map([['ready', { id: 'ready', status: 'success', email: 'test@example.com' }]]),
    autoOpen: new Set(['ready']), launching: new Set(), opened: new Set(), launchPaths: new Map() };
  const launchPath = '/api/passkey/launch/' + 'a'.repeat(43);
  let release, calls = 0;
  const context = { state, toast() {}, renderJobs() {}, refreshOutput: async () => {}, syncEventVisibility() {},
    api(_, options) {
      calls++;
      assert.deepEqual(JSON.parse(options.body), { confirm: 'LAUNCH_PASSKEY', native_window: true });
      return new Promise(resolve => { release = () => resolve({ opened, launch_path: invalid ? 'https://evil.test/' : launchPath }); });
    },
  };
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('  async function openHandoff('), app.indexOf('  function settleAutomaticHandoff(')), context);
  const pending = context.openHandoff('ready');
  await context.openHandoff('ready'); // A repeated SSE event must not duplicate windows.
  assert.equal(calls, 1);
  release(); await pending;
  assert.equal(state.opened.has('ready'), opened && !invalid);
  assert.equal(state.launchPaths.has('ready'), !opened && !invalid);
  assert.equal(state.autoOpen.size, 0);
  assert.equal(state.launching.size, 0);
  if (!invalid) {
    await context.openHandoff('ready');
    assert.equal(calls, 1);
  }
}
async function checkClosePasskeyWindows() {
  const state = { closeWindowsPending: false, opened: new Set(['first', 'second']) };
  let request;
  const context = {
    state,
    renderJobs() {},
    toast(message) { assert.match(message, /2 cửa sổ passkey/); },
    api(path, options) {
      request = { path, options };
      return Promise.resolve({ closed: 2 });
    },
  };
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('  async function closePasskeyWindows()'), app.indexOf('  async function copyText(')), context);
  await context.closePasskeyWindows();
  assert.equal(request.path, '/api/passkey/windows/close');
  assert.equal(request.options.method, 'POST');
  assert.equal(state.opened.size, 0);
  assert.equal(state.closeWindowsPending, false);
}
Promise.all([checkSubmission(), checkSubmission({ fail: true }), checkNativeHandoff(true), checkNativeHandoff(false), checkNativeHandoff(false, true), checkClosePasskeyWindows()])
  .then(() => console.log('passkey append: successive batches, automatic-tab tracking and input preservation passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
