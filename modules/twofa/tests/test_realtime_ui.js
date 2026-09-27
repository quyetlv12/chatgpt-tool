'use strict';

const assert = require('node:assert/strict');
const RealtimeUI = require('../change 2fa community/static/realtime-ui.js');

let scheduled = null;
let scheduledCount = 0;
const flushed = [];
const batcher = RealtimeUI.createEventBatcher({
  delay: 100,
  flush: (events) => flushed.push(events),
  setTimer: (callback) => {
    scheduled = callback;
    scheduledCount += 1;
    return scheduledCount;
  },
  clearTimer: () => {
    scheduled = null;
  },
});

for (let index = 0; index < 20; index += 1) {
  batcher.push({ type: 'job', job: { id: `job-${index}` } });
}

assert.equal(scheduledCount, 1, 'a burst must schedule only one UI flush');
assert.equal(flushed.length, 0, 'events must wait for the batching window');
scheduled();
assert.equal(flushed.length, 1);
assert.equal(flushed[0].length, 20, 'the complete ordered burst must reach one flush');
assert.deepEqual(
  flushed[0].map((event) => event.job.id),
  Array.from({ length: 20 }, (_, index) => `job-${index}`),
  'events must keep their original order',
);

batcher.push({ type: 'worker_health' });
assert.equal(batcher.pendingCount(), 1);
batcher.flushNow();
assert.equal(flushed.length, 2);
assert.equal(batcher.pendingCount(), 0);

batcher.push({ type: 'job', job: { id: 'discarded' } });
batcher.clear();
assert.equal(batcher.pendingCount(), 0);
assert.equal(scheduled, null, 'clear must cancel the pending timer');
assert.equal(flushed.length, 2, 'clear must discard pending events');

const previousRunning = { id: 'change-job', mode: 'change_2fa', status: 'running' };
assert.equal(RealtimeUI.shouldRefreshOutput({
  type: 'job',
  job: { id: 'change-job', mode: 'change_2fa', status: 'success' },
}, previousRunning), true);
assert.equal(RealtimeUI.shouldRefreshOutput({
  type: 'job',
  job: { id: 'check-job', mode: 'check_only', status: 'success' },
}, { status: 'running' }), false);
assert.equal(RealtimeUI.shouldRefreshOutput({
  type: 'job',
  job: { id: 'change-job', mode: 'change_2fa', status: 'success' },
}, { status: 'success' }), false);
assert.equal(RealtimeUI.shouldRefreshOutput({ type: 'worker_health' }, null), false);

console.log('realtime-ui: batching and output invalidation passed');
