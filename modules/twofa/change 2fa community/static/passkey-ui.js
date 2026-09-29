(() => {
  'use strict';

  const state = {
    token: '',
    jobs: new Map(),
    events: null,
    output: '',
    clearPending: false,
    autoOpen: new Set(),
    preopened: new Map(),
    launchPaths: new Map(),
    opened: new Set(),
    launching: new Set(),
  };
  const $ = (id) => document.getElementById(id);
  const icon = (name) => `<svg class="ui-icon" aria-hidden="true"><use href="/assets/tabler-icons.svg?v=1.0.3#ti-${name}"></use></svg>`;

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  }

  function toast(message, type = '') {
    const node = document.createElement('div');
    node.className = `toast ${type}`;
    node.textContent = message;
    $('toast-stack').appendChild(node);
    setTimeout(() => node.remove(), 3800);
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}), 'X-Auth-Token': state.token };
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).detail || message; } catch (_) { /* plain error */ }
      throw new Error(message);
    }
    return await (response.headers.get('content-type')?.includes('json') ? response.json() : response.text());
  }

  function statusLabel(job) {
    return ({ queued: 'ĐANG CHỜ', running: 'ĐANG CHẠY', success: 'SẴN SÀNG', error: 'LỖI', cancelled: 'ĐÃ DỪNG' })[job.status] || job.status;
  }

  function phaseLabel(job) {
    return ({ queued: 'ĐANG CHỜ', authenticating: 'ĐANG ĐĂNG NHẬP', handoff_ready: 'CHỜ MỞ TAB', error: 'LỖI', cancelled: 'ĐÃ DỪNG' })[job.phase] || String(job.phase || '').toUpperCase();
  }

  function renderInputCount() {
    const lines = $('passkey-combo-input').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    $('passkey-line-count').textContent = `${lines.length} bản ghi`;
  }

  function renderOutput() {
    const lines = state.output.trim() ? state.output.trim().split(/\r?\n/) : [];
    $('passkey-success-output').value = lines.join('\n');
    $('passkey-output-count').textContent = `${lines.length} handoff đã cấp`;
    $('copy-passkey-output').disabled = lines.length === 0;
    $('export-passkey-output').disabled = lines.length === 0;
  }

  async function refreshOutput() {
    state.output = await api('/api/passkey/output');
    renderOutput();
  }

  function renderJobs() {
    const jobs = [...state.jobs.values()].sort((a, b) => a.created_at - b.created_at);
    $('clear-passkey-jobs').disabled = state.clearPending;
    $('clear-passkey-jobs').querySelector('span').textContent = state.clearPending ? 'Đang dọn…' : 'Dọn danh sách';
    $('passkey-empty-state').style.display = jobs.length ? 'none' : 'block';
    $('passkey-job-list').innerHTML = jobs.map((job) => {
      const canStop = ['queued', 'running'].includes(job.status);
      const canRetry = ['error', 'cancelled'].includes(job.status) && job.retryable !== false;
      const canDelete = ['success', 'error', 'cancelled'].includes(job.status);
      const launchPath = state.launchPaths.get(job.id);
      const openAction = state.opened.has(job.id)
        ? '<small class="passkey-opened">Đã mở tab</small>'
        : launchPath
        ? `<a class="button passkey-open-link" target="_blank" rel="noopener noreferrer" href="${escapeHtml(launchPath)}">${icon('fingerprint')}<span>Mở lại tab</span></a>`
        : job.handoff_ready
          ? `<button type="button" class="icon-button passkey-open-action" data-passkey-action="open" title="Mở tab OpenAI">${icon('fingerprint')}</button>`
          : job.handoff_issued
            ? '<small class="passkey-opened">Đã mở tab</small>'
            : '';
      return `<tr data-id="${escapeHtml(job.id)}" title="${escapeHtml(job.error || '')}">
        <td class="account"><strong>${escapeHtml(job.email)}</strong><span>${escapeHtml(job.id.slice(0, 10).toUpperCase())}</span></td>
        <td><span class="status ${escapeHtml(job.status)}">${escapeHtml(statusLabel(job))}</span>${job.error ? `<small class="password-row-error">${escapeHtml(job.error)}</small>` : ''}</td>
        <td><span class="password-phase ${escapeHtml(job.phase)}">${escapeHtml(phaseLabel(job))}</span><small>${job.handoff_issued ? 'Tab đã mở; chưa xác nhận enrollment' : job.handoff_ready ? 'Handoff dùng một lần đã sẵn sàng' : 'Đang chuẩn bị'}</small></td>
        <td><div class="row-actions">${openAction}
          <button type="button" class="icon-button" data-passkey-action="logs" title="Xem log">${icon('list-details')}</button>
          ${canRetry ? `<button type="button" class="icon-button" data-passkey-action="retry" title="Chạy lại">${icon('refresh')}</button>` : ''}
          ${canStop ? `<button type="button" class="icon-button" data-passkey-action="stop" title="Dừng">${icon('player-stop')}</button>` : ''}
          ${canDelete ? `<button type="button" class="icon-button" data-passkey-action="delete" title="Xóa">${icon('trash')}</button>` : ''}
        </div></td>
      </tr>`;
    }).join('');
  }

  function preopenTabs(count) {
    const windows = [];
    for (let index = 0; index < count; index += 1) {
      try {
        const popup = window.open('about:blank', '_blank');
        if (popup && !popup.closed) {
          try { popup.opener = null; } catch (_) { /* read-only in some browsers */ }
          windows.push(popup);
        } else {
          windows.push(null);
        }
      } catch (_) {
        windows.push(null);
      }
    }
    return windows;
  }

  async function openHandoff(id, popup = null) {
    if (state.launching.has(id)) return;
    const job = state.jobs.get(id);
    if (!job || job.status !== 'success') return;
    state.launching.add(id);
    const target = popup || state.preopened.get(id) || null;
    try {
      let launchPath = state.launchPaths.get(id);
      if (!launchPath) {
        const data = await api(`/api/passkey/jobs/${encodeURIComponent(id)}/launch`, {
          method: 'POST', body: JSON.stringify({ confirm: 'LAUNCH_PASSKEY' }),
        });
        launchPath = data.launch_path;
        state.launchPaths.set(id, launchPath);
      }
      if (target && !target.closed) {
        target.location.href = new URL(launchPath, window.location.origin).href;
        state.preopened.delete(id);
        state.opened.add(id);
      }
      state.autoOpen.delete(id);
      await refreshOutput();
      renderJobs();
      if (target && !target.closed) toast(`Đã mở tab passkey cho ${job.email}.`);
    } catch (error) {
      if (target && !target.closed) {
        try { target.close(); } catch (_) { /* best effort */ }
      }
      state.preopened.delete(id);
      toast(error.message, 'error');
    } finally {
      state.launching.delete(id);
      renderJobs();
    }
  }

  async function launchJobs() {
    const lines = $('passkey-combo-input').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!lines.length) throw new Error('Hãy nhập ít nhất một tài khoản.');
    const windows = preopenTabs(lines.length);
    const button = $('launch-passkey-jobs');
    button.disabled = true;
    try {
      const data = await api('/api/passkey/jobs', { method: 'POST', body: JSON.stringify({ lines }) });
      data.jobs.forEach((job, index) => {
        state.jobs.set(job.id, job);
        state.autoOpen.add(job.id);
        const popup = windows[index];
        if (popup) state.preopened.set(job.id, popup);
      });
      windows.slice(data.jobs.length).forEach((popup) => { if (popup && !popup.closed) popup.close(); });
      renderJobs();
      toast(`Đã nạp ${data.jobs.length} passkey job; tab sẽ tự mở khi từng tài khoản sẵn sàng.`);
    } catch (error) {
      windows.forEach((popup) => { if (popup && !popup.closed) popup.close(); });
      throw error;
    } finally {
      button.disabled = false;
    }
  }

  async function jobAction(id, action) {
    if (action === 'open') {
      let popup = state.preopened.get(id) || null;
      if (!popup) popup = window.open('about:blank', '_blank');
      await openHandoff(id, popup);
      return;
    }
    if (action === 'logs') {
      const job = state.jobs.get(id);
      $('passkey-log-title').textContent = job?.email || 'Passkey job';
      $('passkey-log-content').textContent = 'Đang tải…';
      $('passkey-logs').showModal();
      const data = await api(`/api/passkey/jobs/${encodeURIComponent(id)}/logs`);
      $('passkey-log-content').textContent = data.logs.join('\n') || 'Chưa có log.';
      return;
    }
    let retryPopup = null;
    if (action === 'retry') {
      try {
        retryPopup = window.open('about:blank', '_blank');
        if (retryPopup && !retryPopup.closed) {
          try { retryPopup.opener = null; } catch (_) { /* read-only in some browsers */ }
          state.preopened.set(id, retryPopup);
        }
      } catch (_) { /* manual fallback remains available */ }
    }
    try {
      if (action === 'delete') {
        await api(`/api/passkey/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
        state.jobs.delete(id);
        state.autoOpen.delete(id);
        state.preopened.delete(id);
        state.launchPaths.delete(id);
        state.opened.delete(id);
      } else {
        const data = await api(`/api/passkey/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
        state.jobs.set(id, data.job);
        if (action === 'retry') state.autoOpen.add(id);
      }
    } catch (error) {
      if (retryPopup && !retryPopup.closed) retryPopup.close();
      state.preopened.delete(id);
      throw error;
    }
    renderJobs();
  }

  async function clearJobs() {
    if (state.clearPending) return;
    state.clearPending = true;
    renderJobs();
    try {
      const data = await api('/api/passkey/jobs', { method: 'DELETE' });
      state.jobs.clear();
      state.autoOpen.clear();
      state.preopened.forEach((popup) => { if (popup && !popup.closed) { try { popup.close(); } catch (_) { /* best effort */ } } });
      state.preopened.clear();
      state.launchPaths.clear();
      state.opened.clear();
      await refreshOutput();
      renderJobs();
      toast(`Đã dọn ${data.deleted} passkey job.`);
    } finally {
      state.clearPending = false;
      renderJobs();
    }
  }

  async function copyText(value) {
    if (!value.trim()) return;
    if (!navigator.clipboard?.writeText) throw new Error('Trình duyệt không hỗ trợ clipboard bảo mật.');
    await navigator.clipboard.writeText(value.trim());
    toast('Đã copy danh sách handoff passkey.');
  }

  async function exportOutput() {
    const response = await fetch('/api/passkey/output', { headers: { 'X-Auth-Token': state.token } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'passkey-handoffs.txt'; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function handleEvent(payload) {
    if (payload.type === 'snapshot') {
      state.jobs.clear();
      payload.jobs.forEach((job) => state.jobs.set(job.id, job));
    } else if (payload.type === 'job') {
      state.jobs.set(payload.job.id, payload.job);
      if (state.autoOpen.has(payload.job.id) && payload.job.status === 'success' && payload.job.handoff_ready) {
        openHandoff(payload.job.id, state.preopened.get(payload.job.id));
      } else if (['error', 'cancelled'].includes(payload.job.status)) {
        const popup = state.preopened.get(payload.job.id);
        if (popup && !popup.closed) { try { popup.close(); } catch (_) { /* best effort */ } }
        state.preopened.delete(payload.job.id);
        state.autoOpen.delete(payload.job.id);
      }
    } else if (payload.type === 'removed') {
      state.jobs.delete(payload.id);
    }
    renderJobs();
  }

  function disconnectEvents() {
    state.events?.close();
    state.events = null;
  }

  function shouldConnectEvents() {
    return Boolean(state.token && document.visibilityState !== 'hidden' && $('passkey-workspace').open);
  }

  function connectEvents() {
    if (!shouldConnectEvents() || state.events) return;
    state.events = new EventSource(`/api/passkey/events?token=${encodeURIComponent(state.token)}`);
    state.events.onmessage = ({ data }) => {
      try { handleEvent(JSON.parse(data)); } catch (error) { console.warn('Bỏ qua passkey event không hợp lệ.', error); }
    };
  }

  function syncEventVisibility() {
    if (shouldConnectEvents()) connectEvents(); else disconnectEvents();
  }

  async function openWorkspace() {
    const data = await api('/api/passkey/bootstrap');
    state.jobs.clear();
    data.jobs.forEach((job) => state.jobs.set(job.id, job));
    renderJobs();
    await refreshOutput();
    if (!$('passkey-workspace').open) $('passkey-workspace').showModal();
    syncEventVisibility();
  }

  function closeWorkspace() {
    disconnectEvents();
    state.preopened.forEach((popup) => { if (popup && !popup.closed) { try { popup.close(); } catch (_) { /* best effort */ } } });
    state.preopened.clear();
    $('passkey-combo-input').value = '';
    renderInputCount();
  }

  async function init() {
    try {
      const bootstrap = await fetch('/api/bootstrap').then((response) => response.json());
      state.token = bootstrap.token;
    } catch (_) { /* main app reports the connection state */ }
  }

  $('open-passkey-tool').addEventListener('click', () => openWorkspace().catch((error) => toast(error.message, 'error')));
  $('close-passkey-tool').addEventListener('click', () => $('passkey-workspace').close());
  $('passkey-workspace').addEventListener('close', closeWorkspace);
  $('passkey-combo-input').addEventListener('input', renderInputCount);
  $('launch-passkey-jobs').addEventListener('click', () => launchJobs().catch((error) => toast(error.message, 'error')));
  $('clear-passkey-jobs').addEventListener('click', () => clearJobs().catch((error) => toast(error.message, 'error')));
  $('copy-passkey-output').addEventListener('click', () => copyText(state.output).catch((error) => toast(error.message, 'error')));
  $('export-passkey-output').addEventListener('click', () => exportOutput().catch((error) => toast(error.message, 'error')));
  $('passkey-job-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-passkey-action]');
    const row = event.target.closest('tr');
    if (button && row) jobAction(row.dataset.id, button.dataset.passkeyAction).catch((error) => toast(error.message, 'error'));
  });
  $('close-passkey-logs').addEventListener('click', () => $('passkey-logs').close());
  $('passkey-logs').addEventListener('close', () => { $('passkey-log-title').textContent = 'Chi tiết'; $('passkey-log-content').textContent = ''; });
  document.addEventListener('visibilitychange', syncEventVisibility);
  window.addEventListener('pagehide', disconnectEvents);

  renderInputCount();
  renderOutput();
  renderJobs();
  init();
})();
