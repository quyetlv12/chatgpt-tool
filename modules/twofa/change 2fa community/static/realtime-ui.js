(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RealtimeUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  'use strict';

  function createEventBatcher({
    flush,
    delay = 100,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  }) {
    if (typeof flush !== 'function') throw new TypeError('flush must be a function');

    let pending = [];
    let timer = null;

    function deliver() {
      if (!pending.length) {
        timer = null;
        return;
      }
      const batch = pending;
      pending = [];
      timer = null;
      flush(batch);
    }

    return {
      push(payload) {
        pending.push(payload);
        if (timer === null) timer = setTimer(deliver, delay);
      },
      flushNow() {
        if (timer !== null) clearTimer(timer);
        deliver();
      },
      clear() {
        if (timer !== null) clearTimer(timer);
        pending = [];
        timer = null;
      },
      pendingCount() {
        return pending.length;
      },
    };
  }

  function shouldRefreshOutput(payload, previousJob) {
    return Boolean(
      payload?.type === 'job'
      && payload.job?.mode === 'change_2fa'
      && payload.job.status === 'success'
      && previousJob?.status !== 'success'
    );
  }

  return { createEventBatcher, shouldRefreshOutput };
});
