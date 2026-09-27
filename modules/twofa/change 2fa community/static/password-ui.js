(() => {
  'use strict';

  const state = {
    token: '',
    configured: false,
    jobs: new Map(),
    output: '',
    history: [],
    events: null,
    clearPending: false,
  };
  const $ = (id) => document.getElementById(id);
  const icon = (name) => `<svg class="ui-icon" aria-hidden="true"><use href="/assets/tabler-icons.svg#ti-${name}"></use></svg>`;

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
      try { message = (await response.json()).detail || message; } catch (_) { /* text error */ }
      throw new Error(message);
    }
    return response.headers.get('content-type')?.includes('json') ? response.json() : response.text();
  }

  function phaseLabel(job) {
    const labels = {
      queued: 'ĐANG CHỜ',
      authenticating: 'ĐANG ĐĂNG NHẬP',
      mutation_started: 'ĐÃ BẮT ĐẦU MUTATION',
      recovery: 'ĐANG ĐỐI SOÁT',
      verified: 'ĐÃ VERIFY LOGIN',
      not_applied: 'CHƯA ÁP DỤNG',
      uncertain: 'CHƯA CHẮC CHẮN',
      error: 'LỖI',
      cancelled: 'ĐÃ DỪNG',
    };
    return labels[job.phase] || String(job.phase || '').toUpperCase();
  }

  function statusLabel(job) {
    const labels = { queued: 'ĐANG CHỜ', running: 'ĐANG CHẠY', success: 'THÀNH CÔNG', error: 'LỖI', cancelled: 'ĐÃ DỪNG' };
    return labels[job.status] || job.status;
  }

  function renderConfigured() {
    const badge = $('password-configured-badge');
    badge.textContent = state.configured ? 'ĐÃ CẤU HÌNH' : 'CHƯA CẤU HÌNH';
    badge.classList.toggle('configured', state.configured);
    $('target-password-status').textContent = state.configured
      ? 'Đang hiển thị mật khẩu chung đã lưu. Sửa trực tiếp rồi bấm Lưu để cập nhật.'
      : 'Chưa có mật khẩu chung. Hãy nhập và lưu trước khi chạy.';
    $('clear-target-password').disabled = !state.configured;
    $('launch-password-jobs').disabled = !state.configured;
  }

  function renderInputCount() {
    const lines = $('password-combo-input').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    $('password-line-count').textContent = `${lines.length} bản ghi`;
  }

  function renderJobs() {
    const jobs = [...state.jobs.values()].sort((a, b) => a.created_at - b.created_at);
    $('clear-password-jobs').disabled = state.clearPending;
    $('clear-password-jobs').setAttribute('aria-busy', String(state.clearPending));
    $('clear-password-jobs').querySelector('span').textContent = state.clearPending ? 'Đang dọn…' : 'Dọn danh sách';
    $('password-empty-state').style.display = jobs.length ? 'none' : 'block';
    $('password-job-list').innerHTML = jobs.map((job) => {
      const canStop = ['queued', 'running'].includes(job.status);
      const canRetry = ['error', 'cancelled'].includes(job.status) && job.retryable !== false;
      const canDelete = ['success', 'error', 'cancelled'].includes(job.status);
      return `<tr data-id="${escapeHtml(job.id)}" title="${escapeHtml(job.error || '')}">
        <td class="account"><strong>${escapeHtml(job.email)}</strong><span>${escapeHtml(job.id.slice(0, 10).toUpperCase())}</span></td>
        <td><span class="status ${escapeHtml(job.status)}">${escapeHtml(statusLabel(job))}</span>${job.error ? `<small class="password-row-error">${escapeHtml(job.error)}</small>` : ''}</td>
        <td><span class="password-phase ${escapeHtml(job.phase)}">${escapeHtml(phaseLabel(job))}</span><small>${job.login_verified ? 'Fresh login thành công' : job.mutation_started ? 'Không auto-resend' : 'Chưa mutation'}</small></td>
        <td><div class="row-actions">
          <button type="button" class="icon-button" data-password-action="logs" title="Xem log">${icon('list-details')}</button>
          ${canRetry ? `<button type="button" class="icon-button" data-password-action="retry" title="Chạy lại">${icon('refresh')}</button>` : ''}
          ${canStop ? `<button type="button" class="icon-button" data-password-action="stop" title="Dừng">${icon('player-stop')}</button>` : ''}
          ${canDelete ? `<button type="button" class="icon-button" data-password-action="delete" title="Xóa">${icon('trash')}</button>` : ''}
        </div></td>
      </tr>`;
    }).join('');
  }

  function renderOutput() {
    const lines = state.output.trim() ? state.output.trim().split(/\r?\n/) : [];
    $('password-success-output').value = lines.join('\n');
    $('password-output-count').textContent = `${lines.length} tài khoản`;
    $('copy-password-output').disabled = lines.length === 0;
    $('export-password-output').disabled = lines.length === 0;
  }

  async function refreshOutput() {
    state.output = await api('/api/password/output');
    renderOutput();
  }

  async function refreshSettingsStatus(reveal = false) {
    if (!state.token) return;
    const data = reveal
      ? await api('/api/password-settings?reveal=true')
      : await api('/api/password-settings');
    state.configured = Boolean(data.configured);
    if (reveal && $('settings-drawer').getAttribute('aria-hidden') === 'false') {
      $('setting-target-password').value = data.target_password || '';
    }
    renderConfigured();
  }

  async function saveTargetPassword(inputId) {
    const input = $(inputId);
    const target = input.value;
    if (!target) throw new Error('Hãy nhập mật khẩu chung mới. Để trống sẽ không thay đổi cấu hình.');
    if (target.length < 12 || target.length > 128) throw new Error('Mật khẩu chung phải dài từ 12 đến 128 ký tự.');
    const data = await api('/api/password-settings', {
      method: 'PUT',
      body: JSON.stringify({ target_password: target }),
    });
    state.configured = Boolean(data.configured);
    input.value = target;
    renderConfigured();
    toast('Đã lưu mật khẩu chung cho các password job mới.');
  }

  async function clearTargetPassword() {
    if (!window.confirm('Xóa mật khẩu chung đã cấu hình? Job đã enqueue vẫn giữ checkpoint riêng.')) return;
    const data = await api('/api/password-settings', { method: 'DELETE' });
    $('setting-target-password').value = '';
    $('password-workspace-target-password').value = '';
    state.configured = Boolean(data.configured);
    renderConfigured();
    toast('Đã xóa cấu hình mật khẩu chung.');
  }

  async function launchJobs() {
    const lines = $('password-combo-input').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!state.configured) throw new Error('Hãy cấu hình mật khẩu chung trong Settings trước.');
    if (!lines.length) throw new Error('Hãy nhập ít nhất một tài khoản.');
    const button = $('launch-password-jobs');
    button.disabled = true;
    try {
      const data = await api('/api/password/jobs', {
        method: 'POST',
        body: JSON.stringify({ lines }),
      });
      data.jobs.forEach((job) => state.jobs.set(job.id, job));
      renderJobs();
      toast(`Đã nạp ${data.jobs.length} password job vào queue riêng.`);
    } finally {
      button.disabled = !state.configured;
    }
  }

  async function jobAction(id, action) {
    if (action === 'logs') {
      const job = state.jobs.get(id);
      $('password-log-title').textContent = job?.email || 'Password job';
      $('password-log-content').textContent = 'Đang tải…';
      $('password-logs').showModal();
      const data = await api(`/api/password/jobs/${encodeURIComponent(id)}/logs`);
      $('password-log-content').textContent = data.logs.join('\n') || 'Chưa có log.';
      return;
    }
    if (action === 'delete') {
      await api(`/api/password/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
      state.jobs.delete(id);
    } else {
      const data = await api(`/api/password/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
      state.jobs.set(id, data.job);
    }
    renderJobs();
  }

  async function clearJobs() {
    if (state.clearPending) return;
    state.clearPending = true;
    renderJobs();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
      const data = await api('/api/password/jobs', { method: 'DELETE', signal: controller.signal });
      state.jobs.clear();
      renderJobs();
      await refreshOutput();
      toast(`Đã dọn ${data.deleted} password job.`);
    } catch (error) {
      throw error.name === 'AbortError'
        ? new Error('Dọn password job quá lâu, hãy kiểm tra lại trạng thái rồi thử lại.')
        : error;
    } finally {
      clearTimeout(timeout);
      state.clearPending = false;
      renderJobs();
    }
  }

  async function copyText(value, successMessage) {
    if (!value.trim()) return;
    if (!navigator.clipboard?.writeText) throw new Error('Trình duyệt không hỗ trợ clipboard bảo mật.');
    await navigator.clipboard.writeText(value.trim());
    toast(successMessage);
  }

  async function exportOutput() {
    const response = await fetch('/api/password/output', { headers: { 'X-Auth-Token': state.token } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'password-success.txt';
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function visibleHistory() {
    return window.PasswordHistoryUI.filterEntries(
      state.history,
      $('password-history-search').value,
    );
  }

  function renderHistory() {
    const filtered = visibleHistory();
    const query = $('password-history-search').value.trim();
    $('password-history-count').textContent = filtered.length;
    $('password-history-total').textContent = query
      ? `${state.history.length} bản ghi tổng cộng`
      : `${state.history.length} bản ghi`;
    $('password-history-copy-label').textContent = query ? 'Copy kết quả' : 'Copy tất cả';
    $('copy-password-history').disabled = filtered.length === 0;

    if (!filtered.length) {
      $('password-history-list').innerHTML = state.history.length
        ? `<div class="history-state password-history-empty">${icon('search')}<strong>Không tìm thấy email</strong><span>Thử một từ khóa ngắn hơn hoặc kiểm tra lại chính tả.</span></div>`
        : '<div class="history-state password-history-empty"><strong>Chưa có lịch sử</strong><span>Bản ghi sẽ xuất hiện sau khi mật khẩu mới được xác minh bằng fresh login.</span></div>';
      return;
    }

    $('password-history-list').innerHTML = filtered.map(({ entry, index }, visibleIndex) => {
      const parts = window.PasswordHistoryUI.credentialParts(entry);
      const changedAt = new Date(Number(entry.changed_at) * 1000);
      const validDate = !Number.isNaN(changedAt.getTime());
      const dateLabel = validDate ? changedAt.toLocaleString('vi-VN') : 'Không rõ thời gian';
      const dateValue = validDate ? changedAt.toISOString() : '';
      return `<article class="password-history-entry">
        <span class="password-history-sequence">${String(visibleIndex + 1).padStart(2, '0')}</span>
        <div class="password-history-card-body">
          <header class="password-history-card-head">
            <strong title="${escapeHtml(parts.email)}">${escapeHtml(parts.email)}</strong>
            <time datetime="${escapeHtml(dateValue)}">${escapeHtml(dateLabel)}</time>
          </header>
          <div class="password-history-fields">
            <div><span>Mật khẩu mới</span><code>${escapeHtml(parts.password || 'Không có dữ liệu')}</code></div>
            <div><span>Khóa 2FA hiện tại</span><code>${escapeHtml(parts.secret || 'Không có dữ liệu')}</code></div>
          </div>
        </div>
        <button type="button" class="password-history-copy" data-password-history-index="${index}" aria-label="Copy bản ghi của ${escapeHtml(parts.email)}" title="Copy bản ghi">${icon('copy')}<span>Copy</span></button>
      </article>`;
    }).join('');
  }

  async function openHistory() {
    const search = $('password-history-search');
    search.value = '';
    search.disabled = true;
    state.history = [];
    $('password-history-count').textContent = '0';
    $('password-history-total').textContent = 'Đang tải';
    $('copy-password-history').disabled = true;
    $('password-history-list').innerHTML = '<div class="history-state">Đang tải lịch sử…</div>';
    $('password-history').showModal();
    try {
      const data = await api('/api/password/history');
      state.history = Array.isArray(data.history) ? data.history : [];
      renderHistory();
    } catch (error) {
      $('password-history-total').textContent = 'Không tải được';
      $('password-history-list').innerHTML = '<div class="history-state error">Không tải được lịch sử đổi mật khẩu.</div>';
      throw error;
    } finally {
      search.disabled = false;
      search.focus({ preventScroll: true });
    }
  }

  function clearHistory() {
    state.history = [];
    $('password-history-search').value = '';
    $('password-history-search').disabled = true;
    $('password-history-count').textContent = '0';
    $('password-history-total').textContent = '0 bản ghi';
    $('password-history-copy-label').textContent = 'Copy tất cả';
    $('copy-password-history').disabled = true;
    $('password-history-list').innerHTML = '<div class="history-state">Lịch sử đã được xóa khỏi phiên giao diện.</div>';
  }

  function clearWorkspaceCredentials() {
    state.output = '';
    $('password-combo-input').value = '';
    $('password-workspace-target-password').value = '';
    renderInputCount();
    renderOutput();
  }

  function clearTargetPasswordInput() {
    $('setting-target-password').value = '';
  }

  function handleEvent(payload) {
    let refresh = false;
    if (payload.type === 'snapshot') {
      state.jobs.clear();
      payload.jobs.forEach((job) => state.jobs.set(job.id, job));
      refresh = true;
    } else if (payload.type === 'job') {
      const previous = state.jobs.get(payload.job.id);
      state.jobs.set(payload.job.id, payload.job);
      refresh = payload.job.status === 'success' && previous?.status !== 'success';
    } else if (payload.type === 'removed') {
      state.jobs.delete(payload.id);
    }
    renderJobs();
    if (refresh) refreshOutput().catch((error) => toast(error.message, 'error'));
  }

  function disconnectEvents() {
    state.events?.close();
    state.events = null;
  }

  function shouldConnectEvents() {
    return Boolean(
      state.token
      && document.visibilityState !== 'hidden'
      && $('password-workspace').open
    );
  }

  function connectEvents() {
    if (!shouldConnectEvents() || state.events) return;
    state.events = new EventSource(`/api/password/events?token=${encodeURIComponent(state.token)}`);
    state.events.onmessage = ({ data }) => {
      try { handleEvent(JSON.parse(data)); }
      catch (error) { console.warn('Bỏ qua password event không hợp lệ.', error); }
    };
  }

  function syncEventVisibility() {
    if (shouldConnectEvents()) connectEvents();
    else disconnectEvents();
  }

  async function openWorkspace() {
    const data = await api('/api/password/bootstrap');
    state.configured = Boolean(data.configured);
    $('password-workspace-target-password').value = data.target_password || '';
    state.jobs.clear();
    data.jobs.forEach((job) => state.jobs.set(job.id, job));
    renderConfigured();
    renderJobs();
    await refreshOutput();
    if (!$('password-workspace').open) $('password-workspace').showModal();
    syncEventVisibility();
  }

  async function init() {
    try {
      const bootstrap = await fetch('/api/bootstrap').then((response) => response.json());
      state.token = bootstrap.token;
      await refreshSettingsStatus();
    } catch (error) {
      $('target-password-status').textContent = 'Không kết nối được password workflow.';
    }
  }

  function closeWorkspace() {
    disconnectEvents();
    clearWorkspaceCredentials();
  }

  $('open-password-tool').addEventListener('click', () => openWorkspace().catch((error) => toast(error.message, 'error')));
  $('close-password-tool').addEventListener('click', () => $('password-workspace').close());
  $('password-workspace').addEventListener('close', closeWorkspace);
  $('password-combo-input').addEventListener('input', renderInputCount);
  $('launch-password-jobs').addEventListener('click', () => launchJobs().catch((error) => toast(error.message, 'error')));
  $('save-target-password').addEventListener('click', () => saveTargetPassword('setting-target-password').catch((error) => toast(error.message, 'error')));
  $('save-workspace-target-password').addEventListener('click', () => saveTargetPassword('password-workspace-target-password').catch((error) => toast(error.message, 'error')));
  $('clear-target-password').addEventListener('click', () => clearTargetPassword().catch((error) => toast(error.message, 'error')));
  $('open-settings').addEventListener('click', () => refreshSettingsStatus(true).catch((error) => toast(error.message, 'error')));
  $('close-settings').addEventListener('click', clearTargetPasswordInput);
  $('drawer-backdrop').addEventListener('click', clearTargetPasswordInput);
  $('copy-password-output').addEventListener('click', () => copyText(state.output, 'Đã copy verified password output.').catch((error) => toast(error.message, 'error')));
  $('export-password-output').addEventListener('click', () => exportOutput().catch((error) => toast(error.message, 'error')));
  $('clear-password-jobs').addEventListener('click', () => clearJobs().catch((error) => toast(error.message, 'error')));
  $('password-job-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-password-action]');
    const row = event.target.closest('tr');
    if (button && row) jobAction(row.dataset.id, button.dataset.passwordAction).catch((error) => toast(error.message, 'error'));
  });
  $('open-password-history').addEventListener('click', () => openHistory().catch((error) => toast(error.message, 'error')));
  $('close-password-history').addEventListener('click', () => $('password-history').close());
  $('password-history').addEventListener('close', clearHistory);
  $('password-history-search').addEventListener('input', renderHistory);
  $('copy-password-history').addEventListener('click', () => {
    const lines = window.PasswordHistoryUI.rawLines(visibleHistory());
    const message = $('password-history-search').value.trim()
      ? `Đã copy ${lines.length} kết quả đang hiển thị.`
      : 'Đã copy toàn bộ lịch sử đổi mật khẩu.';
    copyText(lines.join('\n'), message).catch((error) => toast(error.message, 'error'));
  });
  $('password-history-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-password-history-index]');
    if (!button) return;
    const raw = state.history[Number(button.dataset.passwordHistoryIndex)]?.raw || '';
    copyText(raw, 'Đã copy bản ghi mật khẩu.').catch((error) => toast(error.message, 'error'));
  });
  $('close-password-logs').addEventListener('click', () => $('password-logs').close());
  $('password-logs').addEventListener('close', () => {
    $('password-log-title').textContent = 'Chi tiết';
    $('password-log-content').textContent = '';
  });
  document.addEventListener('visibilitychange', syncEventVisibility);
  window.addEventListener('pagehide', disconnectEvents);

  renderInputCount();
  renderOutput();
  renderJobs();
  init();
})();
