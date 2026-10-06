'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/app.js'), 'utf8');
const slice = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));

(async () => {
  const current = { id: 'same-id', status: 'success', account_state: 'live', login_verified: true };
  const state = { jobs: new Map([[current.id, current]]), journals: [], journal: null,
    journalChoice: '', journalLoading: false, journalSaving: false, journalRequestVersion: 0,
    selectedLogoutJobIds: new Set([current.id]), pendingRechecks: new Set(), pendingRowChangeResults: new Set() };
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', children: [], open: false, setAttribute() {},
      showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
      replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); } });
    return nodes.get(id);
  };
  const calls = [], toasts = [], copied = [];
  let responder = async () => ({});
  const context = { state, $, document: { createElement: () => ({}), querySelectorAll: () => [] },
    LAUNCH_REQUEST_TIMEOUT_MS: 15000, render() {}, renderConnection() {}, refreshOutput() {},
    window: { RealtimeUI: { shouldRefreshOutput: () => false } },
    toast: (message, type) => toasts.push({ message, type }), writeClipboard: async raw => copied.push(raw),
    api: async (url, options) => { calls.push({ url, options }); return responder(url, options); } };
  vm.createContext(context);
  vm.runInContext(slice('  function isJournalView()', '  function renderConnection()')
    + slice('  async function jobAction(', '  async function recheckAccount(')
    + slice('  function flushRealtimeEvents(', '  function disconnectEvents('), context);
  const journal = id => ({ journal: { id, name: '<img onerror=alert(1)>', account_count: 1, created_at: 1 },
    jobs: [{ ...current, email: 'archived@example.test' }] });

  assert.equal(context.liveJournalCount(), 1);
  responder = async () => ({ journals: [] });
  context.openLiveJournal();
  assert.equal($('live-journal-modal').open, true);
  await Promise.resolve();
  const pending = {};
  responder = url => new Promise(resolve => { pending[url] = resolve; });
  const first = context.selectLiveJournal('first');
  assert.equal(context.displayedJobs().length, 0, 'loading must not relabel runtime rows as historical');
  assert.equal(state.filter, 'all', 'opening a journal reveals every saved row');
  context.leaveLiveJournal();
  pending['/api/live-journals/first'](journal('first'));
  await first;
  assert.equal(state.journal, null, 'late reply must not reopen a dismissed journal');
  assert.equal($('live-journal-modal').open, false);
  const older = context.selectLiveJournal('older');
  const newer = context.selectLiveJournal('newer');
  pending['/api/live-journals/newer'](journal('newer')); await newer;
  pending['/api/live-journals/older'](journal('older')); await older;
  assert.equal(state.journal.journal.id, 'newer');
  assert.equal(state.selectedLogoutJobIds.size, 0);
  context.flushRealtimeEvents([{ type: 'snapshot', jobs: [] }]);
  assert.equal(state.jobs.size, 0);
  assert.equal(context.displayedJobs()[0].email, 'archived@example.test', 'SSE must not overwrite the archive');
  const before = calls.length;
  for (const action of ['recheck', 'retry', 'stop', 'delete', 'change-2fa', 'logout-sessions', 'passkey', 'refresh-usage', 'logs']) {
    await context.jobAction('same-id', action);
  }
  assert.equal(calls.length, before, 'archive actions must never mutate runtime jobs');
  responder = async () => 'archived@example.test|SYNTHETIC-PASSWORD|SYNTHETIC-SECRET';
  await context.jobAction('same-id', 'copy-raw');
  assert.equal(calls.at(-1).url, '/api/live-journals/newer/accounts/same-id/raw');
  assert.equal(copied.length, 1);
  context.leaveLiveJournal();
  assert.equal(context.displayedJobs().length, 0, 'return reveals latest real queue, not archived jobs');

  state.jobs.set(current.id, current);
  $('live-journal-name').value = '  Daily  ';
  responder = async (_url, options) => {
    if (options.method === 'POST') return { journal: journal('saved').journal };
    throw new Error('metadata refresh unavailable');
  };
  await context.saveLiveJournal({ preventDefault() {} });
  assert.equal(JSON.parse(calls.at(-2).options.body).name, 'Daily');
  assert.equal(state.jobs.get(current.id), current, 'save never replaces the real queue');
  assert.equal(state.journalSaving, false);
  assert.equal($('live-journal-modal').open, false, 'successful save closes the modal');
  assert.match(toasts.at(-1).message, /Nhật ký đã lưu/);
  assert.doesNotMatch(toasts.at(-1).message, /Chưa hoàn tất/);
  responder = async () => ({ journals: [journal('saved').journal] });
  await context.refreshLiveJournals();
  assert.match($('live-journal-select').children[1].textContent, /<img onerror/);
  assert.equal($('live-journal-select').children[1].innerHTML, undefined, 'journal titles are text, never HTML');
  $('live-journal-modal').showModal();
  $('live-journal-name').value = 'Keep my draft';
  responder = async () => { throw new Error('synthetic storage error'); };
  await context.saveLiveJournal({ preventDefault() {} });
  assert.equal($('live-journal-modal').open, true, 'failed save keeps the form available');
  assert.equal($('live-journal-name').value, 'Keep my draft');
  console.log('live journals UI: immutable view, stale-response races, historical copy, readonly actions and save recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
