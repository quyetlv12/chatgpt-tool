'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../change 2fa community/static');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'dashboard.css'), 'utf8');
for (const id of ['retry-failed', 'clear-failed', 'logout-selected', 'stop-all', 'clear-all']) {
  assert.match(html, new RegExp(`id="${id}"[^>]* hidden`), 'hidden before bootstrap');
}
assert.match(css, /\.queue-tools \.button\[hidden\]\{display:none\}/);
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, { setAttribute() {} });
  return nodes.get(id);
};
const state = { jobs: new Map(), selectedLogoutJobIds: new Set(), pendingRechecks: new Set(), pendingRowChangeResults: new Set(), logoutPending: false, clearAllPending: false };
let archived = false;
const context = { state, $, window: {}, isJournalView: () => archived,
  displayedJobs: () => [...state.jobs.values()],
  isLoggingOut: job => Boolean(job.sessions_logging_out),
  isPasskeyPreparing: job => Boolean(job.passkey_preparing) };
vm.createContext(context);
vm.runInContext(app.slice(app.indexOf('  function counts()'), app.indexOf('  function filteredJobs()'))
  + app.slice(app.indexOf('  function isLogoutEligible(job)'), app.indexOf('  function render()')), context);
function render() {
  if (!state.logoutPending) context.pruneLogoutSelection();
  context.counts();
  context.renderLogoutSelection([...state.jobs.values()]);
}
render();
assert.ok(['retry-failed', 'clear-failed', 'logout-selected'].every(id => $(id).hidden));
assert.equal($('stop-all').hidden, true);
assert.equal($('clear-all').hidden, true);
for (const status of ['queued', 'running']) {
  state.jobs.set('active', { id: 'active', status });
  render();
  assert.equal($('stop-all').hidden, false);
  assert.equal($('stop-all').disabled, false);
  assert.equal($('clear-all').hidden, true);
  archived = true;
  render();
  assert.equal($('stop-all').hidden, true, 'readonly archive cannot stop runtime jobs');
  archived = false;
}
for (const status of ['success', 'error', 'cancelled']) {
  state.jobs.set('active', { id: 'active', status });
  render();
  assert.equal($('stop-all').hidden, true);
  assert.equal($('stop-all').disabled, true);
  assert.equal($('clear-all').hidden, false);
}
for (const flag of ['usage_refreshing', 'chat_deleting', 'sessions_logging_out', 'passkey_preparing']) {
  state.jobs.get('active')[flag] = true;
  render();
  assert.equal($('clear-all').hidden, true, `${flag} blocks clearing`);
  state.jobs.get('active')[flag] = false;
}
for (const pending of [state.pendingRechecks, state.pendingRowChangeResults]) {
  pending.add('active'); render();
  assert.equal($('clear-all').hidden, true);
  pending.clear();
}
state.clearAllPending = true;
state.jobs.clear();
render();
assert.equal($('clear-all').hidden, false, 'keep pending cleanup visible after SSE clears the rows');
assert.equal($('clear-all').disabled, true);
state.clearAllPending = false;
state.jobs.clear();
state.jobs.set('error', { id: 'error', status: 'error', account_state: 'unknown', retryable: false });
render();
assert.equal($('retry-failed').hidden, true, 'nonretryable errors do not show Retry');
assert.equal($('clear-failed').hidden, false, 'clearable error shows Clear');
state.jobs.get('error').retryable = true;
render();
assert.equal($('retry-failed').hidden, false);
state.jobs.clear();
const live = { id: 'live', status: 'success', account_state: 'live' };
state.jobs.set(live.id, live);
render();
assert.equal($('retry-failed').hidden, true);
assert.equal($('clear-failed').hidden, true);
assert.equal($('logout-selected').hidden, true, 'Live rows alone are not a selection');
state.selectedLogoutJobIds.add(live.id);
render();
assert.equal($('logout-selected').hidden, false);
assert.equal($('logout-selected').disabled, false);
state.logoutPending = true;
live.sessions_logging_out = true;
render();
assert.equal($('logout-selected').hidden, false, 'keep pending batch visible');
assert.equal($('logout-selected').disabled, true);
state.logoutPending = false;
render();
assert.equal($('logout-selected').hidden, true, 'ineligible selection is pruned');
state.jobs.set('cancelled', { id: 'cancelled', status: 'cancelled', account_state: 'unknown' });
archived = true;
render();
assert.equal($('clear-all').hidden, true);
assert.equal($('retry-failed').hidden, true);
assert.equal($('clear-failed').hidden, true);
archived = false;
render();
assert.equal($('retry-failed').hidden, false);
assert.equal($('clear-failed').hidden, false);
console.log('queue toolbar: empty, eligibility, selection, pending and archive visibility passed');
