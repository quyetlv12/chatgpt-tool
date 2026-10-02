'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../change 2fa community/static');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
assert.match(app, /data-action="logout-sessions"/);
assert.match(app, /icon-button logout-sessions/);
assert.match(app, /\/api\/jobs\/\$\{encodeURIComponent\(id\)\}\/logout-sessions/);
assert.match(app, /JSON\.stringify\(\{ confirm: 'LOGOUT_ALL_SESSIONS' \}\)/);
assert.match(app, /sessions_logging_out/);
assert.match(app, /selectedLogoutJobIds: new Set/);
assert.match(app, /function isLogoutEligible\(job\)/);
assert.match(app, /function runWithConcurrency\(items, limit, worker\)/);
assert.match(app, /state\.selectedLogoutJobIds\.add/);
assert.match(app, /state\.selectedLogoutJobIds\.delete/);
assert.match(app, /Đang logout \$\{completed\}\/\$\{ids\.length\}/);
assert.match(html, /id="logout-selected"/);
assert.match(html, /id="logout-selected-count"/);
assert.match(html, /id="select-all-logout"/);
assert.match(html, /id="logout-sessions-confirm"[^>]*aria-labelledby="logout-sessions-title"/);
assert.match(html, /30 phút/);
assert.match(html, /app khác[^<]*cùng tài khoản/);
assert.match(app, /Đã gửi logout \$\{succeeded\} tài khoản/);
assert.match(app, /Tool không tự động thử lại/);
assert.match(app, /logout-sessions-cancel[^]*?disabled = true/);

const eligibilitySource = app.slice(
  app.indexOf('  function isLogoutEligible(job)'),
  app.indexOf('  function pruneLogoutSelection()'),
);
const batchSource = app.slice(
  app.indexOf('  async function runWithConcurrency(items, limit, worker)'),
  app.indexOf('  function openDeleteChatsConfirmation(id)'),
);

(async () => {
  const jobs = [
    { id: 'live-a', email: 'a@example.test', status: 'success', account_state: 'live' },
    { id: 'live-b', email: 'b@example.test', status: 'success', account_state: 'live' },
    { id: 'live-c', email: 'c@example.test', status: 'success', account_state: 'live' },
    { id: 'dead', email: 'dead@example.test', status: 'error', account_state: 'die' },
  ];
  const state = {
    jobs: new Map(jobs.map(job => [job.id, job])),
    settings: { 'twofa.max_concurrent': 2 },
    pendingRechecks: new Set(),
    selectedLogoutJobIds: new Set(['live-a', 'live-b', 'live-c']),
    pendingLogoutJobIds: ['live-a', 'live-b', 'live-c'],
    logoutPending: false,
  };
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {
      disabled: false, innerHTML: '', closed: false,
      close() { this.closed = true; },
    });
    return nodes.get(id);
  };
  let active = 0;
  let maxActive = 0;
  const calls = [];
  const toasts = [];
  const context = {
    state, $, icon: () => '', render() {},
    isLoggingOut: job => Boolean(job.sessions_logging_out),
    isPasskeyPreparing: job => Boolean(job.passkey_preparing),
    toast: (message, type) => toasts.push({ message, type }),
    setTimeout, Promise, Math, Number, Boolean,
    api: async (url, options) => {
      calls.push({ url, options });
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise(resolve => setTimeout(resolve, 5));
      active -= 1;
      if (url.includes('live-b')) throw new Error('synthetic failure');
      const id = url.match(/jobs\/([^/]+)/)[1];
      return { job: { ...state.jobs.get(id), sessions_logging_out: false } };
    },
  };
  vm.createContext(context);
  vm.runInContext(eligibilitySource + batchSource, context);

  assert.equal(context.isLogoutEligible(jobs[0]), true);
  assert.equal(context.isLogoutEligible(jobs[3]), false);
  await context.confirmLogoutSessions();
  assert.equal(calls.length, 3, 'one request per selected Live account');
  assert.ok(maxActive <= 2, 'configured concurrency must be respected');
  assert.ok(calls.every(call => call.options.body === JSON.stringify({ confirm: 'LOGOUT_ALL_SESSIONS' })));
  assert.equal(new Set(calls.map(call => call.url)).size, 3, 'failed requests must not retry');
  assert.equal(state.logoutPending, false);
  assert.ok(jobs.slice(0, 3).every(job => state.jobs.get(job.id).sessions_logging_out === false));
  assert.equal($('logout-sessions-confirm').closed, true);
  assert.match(toasts.at(-1).message, /2\/3 tài khoản/);
  console.log('logout-sessions UI: Live-only selection, bounded batch, independent failure and no retry passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
