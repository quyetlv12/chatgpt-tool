(() => {
  'use strict';

  const state = { token: '', jobs: new Map(), filter: 'all', settings: {}, workerHealth: null, connection: 'offline', events: null, eventBatcher: null, output: '', draftTimer: null, pendingLaunch: null, pendingChangeJobId: null, pendingDeleteChatsJobId: null, selectedLogoutJobIds: new Set(), pendingLogoutJobIds: [], pendingPasskeyJobId: null, passkeyPreparing: false, passkeyWindow: null, logoutPending: false, pendingRowChangeResults: new Set(), pendingRechecks: new Set(), activeChangeResultJobId: null, twofaHistory: [], clearAllPending: false };
  const LAUNCH_REQUEST_TIMEOUT_MS = 15000;
  const $ = (id) => document.getElementById(id);
  const icon = (name) => `<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/assets/tabler-icons.svg?v=1.0.3#ti-${name}"></use></svg>`;
  const statusLabels = { queued: 'ĐANG CHỜ', running: 'ĐANG CHẠY', success: 'THÀNH CÔNG', error: 'LỖI', cancelled: 'ĐÃ DỪNG' };
  const errorLabels = {
    account_die: 'TÀI KHOẢN DIE',
    invalid_credentials: 'SAI MẬT KHẨU / 2FA',
    technical_error: 'LỖI KỸ THUẬT',
  };

  function planLabel(job) {
    return job.plan ? String(job.plan).toUpperCase() : 'CHƯA RÕ';
  }

  function statusLabel(job) {
    if (job.status === 'success') return `THÀNH CÔNG · ${planLabel(job)}`;
    if (job.status === 'error' && job.error_kind) return errorLabels[job.error_kind] || 'LỖI';
    return statusLabels[job.status] || job.status;
  }

  function renderConnection() {
    const label = $('connection-label');
    if (!label) return;
    if (state.connection === 'offline') label.textContent = 'SERVER OFFLINE';
    else if (state.connection === 'reconnecting') label.textContent = 'RECONNECTING';
    else if (state.workerHealth?.degraded) label.textContent = 'WORKER DEGRADED';
    else label.textContent = 'LOCAL ONLINE';
  }

  function accountCheck(job) {
    if (job.account_state === 'die') return { label: 'DIE', className: 'die', detail: job.error || 'Tài khoản đã bị vô hiệu hóa' };
    if (job.account_state === 'live') return { label: `LIVE · ${planLabel(job)}`, className: 'live', detail: `Gói kiểm tra từ ${job.plan_source || 'session'}` };
    if (job.error_kind === 'invalid_credentials') return { label: 'CHƯA XÁC MINH', className: 'unknown', detail: job.error || 'Thông tin đăng nhập hoặc 2FA không đúng' };
    return { label: 'CHƯA RÕ', className: 'unknown', detail: job.error || 'Chưa kiểm tra xong tài khoản' };
  }

  async function api(path, options = {}) {
    const { timeoutMs, ...requestOptions } = options;
    const controller = Number.isFinite(timeoutMs) && timeoutMs > 0 ? new AbortController() : null;
    const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    const headers = { ...(requestOptions.headers || {}), 'X-Auth-Token': state.token };
    if (requestOptions.body) headers['Content-Type'] = 'application/json';
    if (controller) requestOptions.signal = controller.signal;
    try {
      const response = await fetch(path, { ...requestOptions, headers });
      if (!response.ok) {
        let message = `HTTP ${response.status}`;
        try { message = (await response.json()).detail || message; } catch (_) { /* plain error */ }
        throw new Error(message);
      }
      return await (response.headers.get('content-type')?.includes('json') ? response.json() : response.text());
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }

  function toast(message, type = '') {
    const node = document.createElement('div');
    node.className = `toast ${type}`;
    node.textContent = message;
    $('toast-stack').appendChild(node);
    setTimeout(() => node.remove(), 3800);
  }

  function updateEditor() {
    const value = $('combo-input').value;
    const count = value.trim() ? value.split(/\r?\n/).filter(Boolean).length : 0;
    $('line-count').textContent = `${count} bản ghi`;
    $('line-numbers').textContent = Array.from({ length: Math.max(1, value.split(/\r?\n/).length) }, (_, i) => i + 1).join('\n');
    document.querySelector('.cursor-hint').style.display = value ? 'none' : 'block';
    syncEditorScroll();
  }

  function syncEditorScroll() {
    const input = $('combo-input');
    const gutter = $('line-numbers');
    gutter.scrollTop = input.scrollTop;
    const lineHeight = Number.parseFloat(getComputedStyle(input).lineHeight) || 20;
    const firstVisibleLine = Math.max(1, Math.floor(input.scrollTop / lineHeight) + 1);
    $('editor-review-state').textContent = input.scrollTop > 1
      ? `Đang xem từ dòng ${firstVisibleLine}`
      : 'Đang hiển thị từ dòng 1';
  }

  function scrollEditorToTop(moveCaret = false) {
    const input = $('combo-input');
    if (moveCaret) input.setSelectionRange(0, 0);
    input.scrollTop = 0;
    input.scrollLeft = 0;
    $('line-numbers').scrollTop = 0;
    syncEditorScroll();
  }

  function handleComboPaste(event) {
    const pastedText = event.clipboardData?.getData('text') || '';
    if (!pastedText.includes('\n')) return;
    requestAnimationFrame(() => {
      scrollEditorToTop(true);
      requestAnimationFrame(() => scrollEditorToTop(true));
    });
  }

  function modeValue() {
    return $('change-enabled').checked ? 'change_2fa' : 'check_only';
  }

  function renderMode() {
    const enabled = $('change-enabled').checked;
    $('change-mode-control').classList.toggle('is-change', enabled);
    $('change-mode-control').classList.toggle('is-check', !enabled);
    $('change-mode-label').textContent = enabled ? 'BẬT · SẼ ĐỔI 2FA' : 'TẮT · CHỈ CHECK';
  }

  function renderInspectionOptions() {
    [
      ['read-usage-enabled', 'read-usage-control'],
      ['read-payment-enabled', 'read-payment-control'],
    ].forEach(([inputId, controlId]) => {
      const enabled = $(inputId).checked;
      $(controlId).classList.toggle('is-enabled', enabled);
      $(controlId).classList.toggle('is-disabled', !enabled);
    });
  }

  async function saveInspectionOption(inputId, settingKey, label) {
    const input = $(inputId);
    const previous = Boolean(state.settings[settingKey]);
    state.settings[settingKey] = input.checked;
    renderInspectionOptions();
    try {
      const data = await api('/api/settings', { method: 'PUT', body: JSON.stringify(settingsPayload()) });
      state.settings = data.settings;
      loadSettingsForm();
      toast(`${label}: ${input.checked ? 'đã bật' : 'đã tắt'}.`);
    } catch (error) {
      state.settings[settingKey] = previous;
      input.checked = previous;
      renderInspectionOptions();
      toast(error.message, 'error');
    }
  }

  function scheduleDraftSave() {
    updateEditor();
    clearTimeout(state.draftTimer);
    state.draftTimer = setTimeout(async () => {
      state.settings['twofa.input_draft'] = $('combo-input').value;
      try {
        const data = await api('/api/settings', { method: 'PUT', body: JSON.stringify(settingsPayload()) });
        state.settings = data.settings;
      } catch (error) {
        toast(`Không lưu được danh sách nháp: ${error.message}`, 'error');
      }
    }, 450);
  }

  function counts() {
    const jobs = [...state.jobs.values()];
    const running = jobs.filter((job) => ['queued', 'running'].includes(job.status)).length;
    const success = jobs.filter((job) => job.status === 'success').length;
    const error = jobs.filter((job) => ['error', 'cancelled'].includes(job.status)).length;
    const clearableErrors = jobs.filter(isClearableFailure).length;
    const lowUsage = jobs.filter((job) => window.UsageUI?.isBelowUsageThreshold(job.usage, 50)).length;
    const fullUsage = jobs.filter((job) => window.UsageUI?.isUsageFullyUsed(job.usage)).length;
    const freeWithoutUsage = jobs.filter((job) => window.UsageUI?.isFreeWithoutUsage(job)).length;
    const retryableErrors = jobs.filter((job) => ['error', 'cancelled'].includes(job.status) && job.retryable !== false).length;
    $('metric-running').textContent = running;
    $('metric-success').textContent = success;
    $('metric-errors').textContent = error;
    $('count-all').textContent = jobs.length;
    $('count-running').textContent = running;
    $('count-success').textContent = success;
    $('count-usage-low').textContent = lowUsage;
    $('count-usage-full').textContent = fullUsage;
    $('count-free-no-usage').textContent = freeWithoutUsage;
    $('count-error').textContent = error;
    $('retry-failed-count').textContent = retryableErrors;
    $('retry-failed').disabled = retryableErrors === 0;
    $('clear-failed-count').textContent = clearableErrors;
    $('clear-failed').disabled = clearableErrors === 0;
  }

  function isClearableFailure(job) {
    return ['error', 'cancelled'].includes(job.status) && job.account_state !== 'live';
  }

  function filteredJobs() {
    const jobs = [...state.jobs.values()].sort((a, b) => a.created_at - b.created_at);
    if (state.filter === 'running') return jobs.filter((j) => ['queued', 'running'].includes(j.status));
    if (state.filter === 'error') return jobs.filter((j) => ['error', 'cancelled'].includes(j.status));
    if (state.filter === 'success') return jobs.filter((j) => j.status === 'success');
    if (state.filter === 'usage-low') return jobs.filter((j) => window.UsageUI?.isBelowUsageThreshold(j.usage, 50));
    if (state.filter === 'usage-full') return jobs.filter((j) => window.UsageUI?.isUsageFullyUsed(j.usage));
    if (state.filter === 'free-no-usage') return jobs.filter((j) => window.UsageUI?.isFreeWithoutUsage(j));
    return jobs;
  }

  function usageCell(job) {
    if (job.usage_enabled === false) {
      return '<div class="usage-placeholder disabled"><span>Đã tắt</span><small>Bỏ qua đọc Usage</small></div>';
    }
    const view = window.UsageUI?.buildUsageView(job.usage);
    if (view) {
      const credit = view.creditLabel
        ? `<span class="usage-credit">${escapeHtml(view.creditLabel)}</span>`
        : '';
      const resetCredit = view.resetCreditLabel
        ? `<span class="usage-reset-credit">${escapeHtml(view.resetCreditLabel)}</span>`
        : '';
      return `<div class="usage-card ${escapeHtml(view.tone)}" aria-label="Usage tuần: ${escapeHtml(view.usedLabel)} đã dùng">
        <div class="usage-line"><strong>${escapeHtml(view.usedLabel)}</strong><span>${escapeHtml(view.remainingLabel)}</span></div>
        <div class="usage-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${view.used}"><i style="width:${view.used}%"></i></div>
        <div class="usage-meta"><small>${escapeHtml(view.statusLabel)}</small><span class="usage-badges">${resetCredit}${credit}</span></div>
      </div>`;
    }
    if (job.status === 'queued') {
      return '<div class="usage-placeholder loading"><i></i><span>Chờ worker…</span></div>';
    }
    if (job.status === 'running') {
      return '<div class="usage-placeholder loading"><i></i><span>Đang đọc Usage…</span></div>';
    }
    if (job.usage_refreshing) {
      return '<div class="usage-placeholder loading"><i></i><span>Đang đọc lại Usage…</span></div>';
    }
    if (job.status === 'success' && job.account_state === 'live') {
      return `<div class="usage-placeholder unavailable"><span>Không đọc được Usage</span><small>Tài khoản vẫn được xác minh</small><button type="button" class="usage-retry" data-action="refresh-usage" ${job.chat_deleting || isLoggingOut(job) ? 'disabled' : ''}>${icon('refresh')}<span>Đọc lại Usage</span></button></div>`;
    }
    return '<div class="usage-placeholder"><span>Chưa có dữ liệu</span></div>';
  }

  function paymentCell(job) {
    if (job.payment_methods_enabled === false) {
      return '<div class="payment-placeholder disabled"><span>Đã tắt</span><small>Bỏ qua đọc Payment</small></div>';
    }
    const view = window.PaymentUI?.buildPaymentView(job.payment_methods, job.billing_date);
    if (view?.empty) {
      return '<div class="payment-placeholder empty"><span>Không có phương thức</span><small>Chưa lưu thanh toán</small></div>';
    }
    if (view) {
      const methods = view.methods.map((method) => {
        const detail = method.detailLabel
          ? `<span>${escapeHtml(method.detailLabel)}</span>`
          : '';
        return `<div class="payment-method">
          <div><strong>${escapeHtml(method.brandLabel || method.typeLabel)}</strong>${method.isDefault ? '<b>Mặc định</b>' : ''}</div>
          ${detail}
        </div>`;
      }).join('');
      const extra = view.extraCount ? `<small class="payment-extra">+${view.extraCount} phương thức khác</small>` : '';
      const paymentDate = view.paymentDateLabel ? `<small class="payment-date">${escapeHtml(view.paymentDateLabel)}</small>` : '';
      return `<div class="payment-card">${methods}${extra}${paymentDate}</div>`;
    }
    if (job.status === 'queued') {
      return '<div class="payment-placeholder loading"><i></i><span>Chờ worker…</span></div>';
    }
    if (job.status === 'running') {
      return '<div class="payment-placeholder loading"><i></i><span>Đang đọc…</span></div>';
    }
    if (job.status === 'success' && job.account_state === 'live') {
      return '<div class="payment-placeholder unavailable"><span>Không đọc được</span><small>Không ảnh hưởng xác minh</small></div>';
    }
    return '<div class="payment-placeholder"><span>Chưa có dữ liệu</span></div>';
  }

  function isLogoutEligible(job) {
    return Boolean(job)
      && job.status === 'success'
      && job.account_state === 'live'
      && !job.chat_deleting
      && !job.usage_refreshing
      && !state.pendingRechecks.has(job.id)
      && !isLoggingOut(job)
      && !isPasskeyPreparing(job);
  }

  function pruneLogoutSelection() {
    state.selectedLogoutJobIds.forEach((id) => {
      if (!isLogoutEligible(state.jobs.get(id))) state.selectedLogoutJobIds.delete(id);
    });
  }

  function renderLogoutSelection(visibleJobs) {
    const eligibleVisibleJobs = visibleJobs.filter(isLogoutEligible);
    const selectedVisibleCount = eligibleVisibleJobs.filter((job) => state.selectedLogoutJobIds.has(job.id)).length;
    const selectAll = $('select-all-logout');
    selectAll.disabled = state.logoutPending || eligibleVisibleJobs.length === 0;
    selectAll.checked = eligibleVisibleJobs.length > 0 && selectedVisibleCount === eligibleVisibleJobs.length;
    selectAll.indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < eligibleVisibleJobs.length;

    const selectedCount = state.selectedLogoutJobIds.size;
    $('logout-selected-count').textContent = selectedCount;
    $('logout-selected').disabled = state.logoutPending || selectedCount === 0;
    $('logout-selected').setAttribute('aria-busy', String(state.logoutPending));
  }

  function render() {
    if (!state.logoutPending) pruneLogoutSelection();
    counts();
    const clearButton = $('clear-all');
    clearButton.disabled = state.clearAllPending;
    clearButton.setAttribute('aria-busy', String(state.clearAllPending));
    clearButton.querySelector('span').textContent = state.clearAllPending ? 'Đang dọn…' : 'Dọn danh sách';
    const jobs = filteredJobs();
    const exportMeta = window.UsageUI?.buildFilteredExportMeta(state.filter, jobs.length);
    $('export-filtered').disabled = jobs.length === 0;
    $('export-filtered-count').textContent = jobs.length;
    $('export-filtered').title = exportMeta?.label || 'Xuất dữ liệu của tab đang chọn';
    $('export-filtered').setAttribute('aria-label', exportMeta?.label || 'Xuất dữ liệu của tab đang chọn');
    $('empty-state').style.display = jobs.length ? 'none' : 'grid';
    $('job-list').innerHTML = jobs.map((job) => {
      const check = accountCheck(job);
      const checkpoint = job.mode === 'check_only' && job.status === 'success'
        ? 'ĐÃ CHECK LIVE'
        : job.login_verified
          ? '2FA ĐÃ XÁC MINH'
          : job.rotated_pending_verify
            ? 'ĐÃ LƯU · CHỜ VERIFY'
            : check.detail;
      const canRetry = ['error', 'cancelled'].includes(job.status) && job.retryable !== false;
      const canStop = ['queued', 'running'].includes(job.status);
      const canChangeTwoFA = isRowChangeEligible(job);
      const canRecheck = job.status === 'success';
      const canDeleteChats = job.status === 'success' && job.account_state === 'live';
      const recheckPending = state.pendingRechecks.has(job.id);
      const chatDeleting = job.chat_deleting === true;
      const loggingOut = isLoggingOut(job);
      const passkeyPreparing = isPasskeyPreparing(job);
      const accountBusy = chatDeleting || loggingOut || passkeyPreparing;
      const canSelectLogout = isLogoutEligible(job);
      const logoutSelected = state.selectedLogoutJobIds.has(job.id);
      return `<tr data-id="${job.id}" title="${escapeHtml(job.error || '')}">
        <td class="logout-select-cell"><input type="checkbox" data-logout-select aria-label="Chọn ${escapeHtml(job.email)} để logout tất cả phiên" ${logoutSelected ? 'checked' : ''} ${canSelectLogout && !state.logoutPending ? '' : 'disabled'}></td>
        <td class="account"><strong>${escapeHtml(job.email)}</strong><span>${job.id.slice(0, 10).toUpperCase()} · <b class="job-mode ${job.mode === 'check_only' ? 'check' : 'change'}">${job.mode === 'check_only' ? 'CHỈ CHECK' : 'ĐỔI 2FA'}</b></span></td>
        <td><span class="status ${job.status} ${job.plan ? `plan-${escapeHtml(job.plan)}` : ''}">${escapeHtml(statusLabel(job))}</span></td>
        <td><div class="account-result"><span class="account-badge ${check.className}">${escapeHtml(check.label)}</span><small>${escapeHtml(checkpoint)}</small></div></td>
        <td class="usage-cell">${usageCell(job)}</td>
        <td class="payment-cell">${paymentCell(job)}</td>
        <td><div class="row-actions">
          <button type="button" class="icon-button copy-raw" data-action="copy-raw" title="Copy raw" aria-label="Copy raw của ${escapeHtml(job.email)}">${icon('copy')}</button>
          ${canChangeTwoFA ? `<button type="button" class="icon-button change-2fa" data-action="change-2fa" title="Đổi 2FA ngay" aria-label="Đổi 2FA cho ${escapeHtml(job.email)}" ${accountBusy ? 'disabled' : ''}>${icon('key')}</button>` : ''}
          ${canDeleteChats ? `<button type="button" class="icon-button delete-chats ${chatDeleting ? 'is-pending' : ''}" data-action="delete-chats" title="Xóa hết dữ liệu chat" aria-label="Xóa hết dữ liệu chat của ${escapeHtml(job.email)}" ${accountBusy ? 'disabled' : ''}>${icon(chatDeleting ? 'loader-2' : 'trash-x')}</button>` : ''}
          ${canDeleteChats ? `<button type="button" class="icon-button logout-sessions ${loggingOut ? 'is-pending' : ''}" data-action="logout-sessions" title="Logout all sessions" aria-label="Logout all sessions của ${escapeHtml(job.email)}" ${accountBusy || job.usage_refreshing || recheckPending ? 'disabled' : ''}>${icon(loggingOut ? 'loader-2' : 'logout')}</button>` : ''}
          ${canDeleteChats && job.login_verified ? `<button type="button" class="icon-button passkey ${passkeyPreparing ? 'is-pending' : ''}" data-action="passkey" title="Thêm passkey" aria-label="Thêm passkey cho ${escapeHtml(job.email)}" ${accountBusy || job.usage_refreshing || recheckPending ? 'disabled' : ''}>${icon(passkeyPreparing ? 'loader-2' : 'fingerprint')}</button>` : ''}
          ${canRecheck ? `<button type="button" class="icon-button recheck-account ${recheckPending ? 'is-pending' : ''}" data-action="recheck" title="Check lại tài khoản" aria-label="Check lại tài khoản ${escapeHtml(job.email)}" ${recheckPending || job.usage_refreshing || accountBusy ? 'disabled' : ''}>${icon(recheckPending ? 'loader-2' : 'shield-check')}</button>` : ''}
          <button type="button" class="icon-button" data-action="logs" title="Xem log" aria-label="Xem log của ${escapeHtml(job.email)}">${icon('list-details')}</button>
          ${canRetry ? `<button type="button" class="icon-button" data-action="retry" title="Thử lại" aria-label="Thử lại ${escapeHtml(job.email)}">${icon('refresh')}</button>` : ''}
          ${canStop ? `<button type="button" class="icon-button" data-action="stop" title="Dừng" aria-label="Dừng ${escapeHtml(job.email)}">${icon('player-stop')}</button>` : ''}
          ${!canStop ? `<button type="button" class="icon-button" data-action="delete" title="Xóa" aria-label="Xóa ${escapeHtml(job.email)}" ${accountBusy ? 'disabled' : ''}>${icon('trash')}</button>` : ''}
        </div></td></tr>`;
    }).join('');
    renderLogoutSelection(jobs);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  }

  function isRowChangeEligible(job) {
    return job.mode === 'check_only' && job.status === 'success' && job.account_state === 'live';
  }

  function renderOutput() {
    const lines = state.output.trim() ? state.output.trim().split(/\r?\n/) : [];
    $('success-output').value = lines.join('\n');
    $('output-line-numbers').textContent = Array.from({ length: Math.max(1, lines.length) }, (_, i) => i + 1).join('\n');
    $('output-count').textContent = `${lines.length} tài khoản`;
    $('output-empty').style.display = lines.length ? 'none' : 'flex';
    $('success-output').style.visibility = lines.length ? 'visible' : 'hidden';
    $('output-line-numbers').style.visibility = lines.length ? 'visible' : 'hidden';
    $('copy-output').disabled = !lines.length;
    $('export-output').disabled = !lines.length;
  }

  function syncOutputScroll() {
    $('output-line-numbers').scrollTop = $('success-output').scrollTop;
  }

  async function refreshOutput() {
    try {
      state.output = await api('/api/output');
      renderOutput();
    } catch (error) {
      toast(`Không tải được output: ${error.message}`, 'error');
    }
  }

  async function writeClipboard(text) {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) {
        // Fall back when a delayed localhost response outlives user activation.
      }
    }
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;left:-9999px;opacity:0';
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand('copy');
    field.remove();
    if (!copied) throw new Error('Trình duyệt không cho phép copy.');
  }

  async function copyOutput() {
    if (!state.output.trim()) return;
    try {
      await writeClipboard(state.output.trim());
      toast('Đã copy toàn bộ tài khoản thành công.');
    } catch (_) {
      toast('Không thể copy tự động. Hãy chọn nội dung và copy thủ công.', 'error');
    }
  }

  function openLaunchConfirmation() {
    const lines = $('combo-input').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!lines.length) return toast('Hãy nhập ít nhất một combo.', 'error');
    const changing = modeValue() === 'change_2fa';
    state.pendingLaunch = { lines, mode: modeValue() };
    $('confirm-accent').className = `confirm-accent ${changing ? 'is-change' : 'is-check'}`;
    $('confirm-icon').innerHTML = changing ? icon('key') : icon('shield-check');
    $('confirm-title').textContent = changing ? 'Kiểm tra và Change 2FA' : 'Chỉ kiểm tra tài khoản';
    $('confirm-message').textContent = changing
      ? 'Hệ thống sẽ kiểm tra Live, Free/Plus, sau đó thay khóa 2FA và đăng nhập lại để xác minh.'
      : 'Bạn chỉ muốn kiểm tra tài khoản Live, Free hoặc Plus. 2FA sẽ không bị thay đổi.';
    $('confirm-count').textContent = lines.length;
    $('confirm-concurrency').textContent = $('quick-concurrency').value;
    $('confirm-action').textContent = changing ? 'SẼ CHANGE 2FA' : 'KHÔNG THAY ĐỔI';
    $('confirm-launch').className = `button ${changing ? 'confirm-change' : 'confirm-check'}`;
    $('confirm-launch').innerHTML = changing
      ? `${icon('key')}<span>Xác nhận Change 2FA</span>`
      : `${icon('shield-check')}<span>Đúng, chỉ kiểm tra</span>`;
    $('launch-confirm').showModal();
  }

  async function launch() {
    if (!state.pendingLaunch) return;
    const { lines, mode } = state.pendingLaunch;
    state.pendingLaunch = null;
    $('launch-confirm').close();
    $('launch-batch').disabled = true;
    try {
      const data = await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({ lines, mode }),
        timeoutMs: LAUNCH_REQUEST_TIMEOUT_MS,
      });
      data.jobs.forEach((job) => state.jobs.set(job.id, job));
      render();
      toast(mode === 'change_2fa'
        ? `Đã nạp ${data.jobs.length} tài khoản để Change 2FA.`
        : `Đã nạp ${data.jobs.length} tài khoản, chỉ kiểm tra Live/Free/Plus.`);
      $('queue').scrollIntoView({ behavior: 'smooth' });
    } catch (error) {
      if (error?.name === 'AbortError') {
        toast('Khởi chạy quá thời gian chờ. Hãy kiểm tra hàng chờ trước khi bấm lại.', 'error');
      } else {
        toast(error.message, 'error');
      }
    }
    finally { $('launch-batch').disabled = false; }
  }

  async function jobAction(id, action) {
    try {
      if (action === 'logs') return openLogs(id);
      if (action === 'change-2fa') return openRowChangeConfirmation(id);
      if (action === 'refresh-usage') {
        await refreshUsage(id);
        return;
      }
      if (action === 'recheck') {
        await recheckAccount(id);
        return;
      }
      if (action === 'delete-chats') return openDeleteChatsConfirmation(id);
      if (action === 'logout-sessions') return openLogoutConfirmation(id);
      if (action === 'passkey') return openPasskeyConfirmation(id);
      if (action === 'copy-raw') {
        await copyRaw(id);
        return;
      }
      if (action === 'retry' || action === 'stop') {
        const data = await api(`/api/jobs/${id}/${action}`, { method: 'POST' });
        state.jobs.set(id, data.job); render();
      } else if (action === 'delete') {
        await api(`/api/jobs/${id}`, { method: 'DELETE' });
        state.pendingRechecks.delete(id);
        state.selectedLogoutJobIds.delete(id);
        state.jobs.delete(id); render();
      }
    } catch (error) { toast(error.message, 'error'); }
  }

  async function recheckAccount(id) {
    const current = state.jobs.get(id);
    if (!current || current.status !== 'success' || state.pendingRechecks.has(id)) return;
    state.pendingRechecks.add(id);
    render();
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/recheck`, { method: 'POST' });
      state.jobs.set(id, data.job);
      toast('Đã đưa tài khoản vào hàng chờ check lại.');
    } finally {
      state.pendingRechecks.delete(id);
      render();
    }
  }

  async function refreshUsage(id) {
    const current = state.jobs.get(id);
    if (!current || current.usage_refreshing) return;
    state.jobs.set(id, { ...current, usage_refreshing: true });
    render();
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/refresh-usage`, { method: 'POST' });
      state.jobs.set(id, data.job);
      render();
      toast('Đã đọc lại Usage thành công.');
    } catch (error) {
      const latest = state.jobs.get(id);
      if (latest) state.jobs.set(id, { ...latest, usage_refreshing: false });
      render();
      throw error;
    }
  }

  function isLoggingOut(job) {
    return [...state.jobs.values()].some((other) => other.sessions_logging_out
      && other.email.trim().toLowerCase() === job.email.trim().toLowerCase());
  }

  function openLogoutConfirmation(id) {
    openLogoutConfirmationForIds([id]);
  }

  function openSelectedLogoutConfirmation() {
    openLogoutConfirmationForIds([...state.selectedLogoutJobIds]);
  }

  function openLogoutConfirmationForIds(ids) {
    const jobs = ids.map((id) => state.jobs.get(id)).filter(isLogoutEligible);
    if (!jobs.length || jobs.length !== ids.length) {
      pruneLogoutSelection();
      render();
      throw new Error('Tài khoản đã chọn chưa đủ điều kiện logout all sessions.');
    }
    state.pendingLogoutJobIds = jobs.map((job) => job.id);
    const visibleEmails = jobs.slice(0, 8).map((job) => job.email);
    if (jobs.length > visibleEmails.length) visibleEmails.push(`… và ${jobs.length - visibleEmails.length} tài khoản khác`);
    $('logout-sessions-title').textContent = jobs.length === 1
      ? 'Logout all sessions'
      : `Logout all sessions · ${jobs.length} tài khoản`;
    $('logout-sessions-description').textContent = jobs.length === 1
      ? 'Đăng xuất các phiên của tài khoản này trên mọi thiết bị, bao gồm phiên hiện tại. Có thể mất tối đa 30 phút. Những tool/app khác đang dùng cùng tài khoản có thể bị ngắt phiên; hãy dừng công việc trên tài khoản này trước khi xác nhận.'
      : `Đăng xuất các phiên của ${jobs.length} tài khoản đã chọn trên mọi thiết bị, bao gồm phiên hiện tại. Có thể mất tối đa 30 phút. Những tool/app khác đang dùng các tài khoản này có thể bị ngắt phiên; hãy dừng công việc trước khi xác nhận.`;
    $('logout-sessions-email').textContent = visibleEmails.join('\n');
    $('logout-sessions-confirm-action').innerHTML = `${icon('logout')}<span>${jobs.length === 1 ? 'Xác nhận logout tất cả' : `Logout ${jobs.length} tài khoản`}</span>`;
    $('logout-sessions-confirm').showModal();
  }

  function isPasskeyPreparing(job) {
    return [...state.jobs.values()].some((other) => other.passkey_preparing
      && other.email.trim().toLowerCase() === job.email.trim().toLowerCase());
  }

  function openPasskeyConfirmation(id) {
    const job = state.jobs.get(id);
    if (!job || job.status !== 'success' || job.account_state !== 'live'
      || !job.login_verified || job.usage_refreshing || job.chat_deleting || isLoggingOut(job) || isPasskeyPreparing(job)) {
      throw new Error('Tài khoản chưa đủ điều kiện thêm passkey.');
    }
    state.pendingPasskeyJobId = id;
    $('passkey-email').textContent = job.email;
    $('passkey-note').textContent = 'Chưa xác nhận đã thêm passkey. Không bật Advanced Account Security trong luồng này. Dừng thao tác khác trên cùng tài khoản khi trang OpenAI đang mở.';
    $('passkey-confirm').showModal();
  }

  function preparePasskeyWindow() {
    try {
      // Open synchronously from the confirmation click so popup blockers treat
      // the later OpenAI redirect as part of the user's explicit action.
      const popup = window.open('about:blank', '_blank');
      if (popup && !popup.closed) {
        try { popup.opener = null; } catch (_) { /* browser may expose it read-only */ }
        return popup;
      }
    } catch (_) { /* use the visible manual-link fallback */ }
    return null;
  }

  function navigatePasskeyWindow(popup, launchPath) {
    if (!popup || popup.closed) return false;
    try {
      popup.location.href = new URL(launchPath, window.location.origin).href;
      return true;
    } catch (_) {
      return false;
    }
  }

  async function confirmPasskey() {
    const id = state.pendingPasskeyJobId;
    const current = id ? state.jobs.get(id) : null;
    if (!id || !current || state.passkeyPreparing || isPasskeyPreparing(current)) return;
    state.passkeyPreparing = true;
    const launch = $('passkey-open');
    state.passkeyWindow = preparePasskeyWindow();
    launch.hidden = true;
    launch.removeAttribute('href');
    state.jobs.set(id, { ...current, passkey_preparing: true });
    const button = $('passkey-confirm-action');
    const timeoutSeconds = Math.min(Number(state.settings['twofa.job_timeout']) || 180, 180);
    let responseLost = false;
    button.disabled = true;
    $('passkey-cancel').disabled = true;
    button.innerHTML = `${icon('loader-2')}<span>Đang chuẩn bị…</span>`;
    $('passkey-note').textContent = `Đang đăng nhập lại và lấy liên kết đăng ký từ OpenAI (giới hạn ${timeoutSeconds} giây; kết nối đang chạy có thể cần thêm thời gian để dừng). Chưa thêm passkey; không chạy thao tác khác trên tài khoản này.`;
    try {
      render();
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/passkey/start`, {
        method: 'POST', body: JSON.stringify({ confirm: 'ADD_PASSKEY' }),
        timeoutMs: (timeoutSeconds + 30) * 1000,
      });
      if (!/^\/api\/passkey\/launch\/[A-Za-z0-9_-]{43}$/.test(data.launch_path)) {
        throw new Error('Liên kết passkey không hợp lệ.');
      }
      const openedAutomatically = navigatePasskeyWindow(state.passkeyWindow, data.launch_path);
      state.passkeyWindow = null;
      if (openedAutomatically) {
        launch.hidden = true;
        launch.removeAttribute('href');
        $('passkey-note').textContent = 'Đã tự mở tab OpenAI. Hoàn tất passkey trên tab đó rồi đóng trang trước khi chạy thao tác khác. Tool chưa xác nhận đã thêm passkey.';
        toast('Đã tự mở tab OpenAI để hoàn tất passkey.');
      } else {
        launch.href = data.launch_path;
        launch.hidden = false;
        $('passkey-note').textContent = 'Trình duyệt đã chặn mở tab tự động. Bấm liên kết bên dưới để mở OpenAI; hoàn tất rồi đóng trang trước khi chạy thao tác khác. Tool chưa xác nhận đã thêm passkey.';
        toast('Trình duyệt chặn popup; hãy bấm liên kết OpenAI để tiếp tục.', 'error');
      }
    } catch (error) {
      responseLost = error?.name === 'AbortError' || error instanceof TypeError;
      const message = error?.name === 'AbortError'
        ? 'Chuẩn bị passkey quá thời gian chờ. Chưa xác nhận kết quả; chờ tài khoản hết trạng thái chuẩn bị trước khi thử lại.'
        : error.message;
      $('passkey-note').textContent = message;
      toast(message, 'error');
    } finally {
      const latest = state.jobs.get(id);
      if (latest && !responseLost) state.jobs.set(id, { ...latest, passkey_preparing: false });
      state.passkeyPreparing = false;
      button.disabled = false;
      $('passkey-cancel').disabled = false;
      button.innerHTML = `${icon('fingerprint')}<span>Chuẩn bị thêm passkey</span>`;
      if (state.passkeyWindow && !state.passkeyWindow.closed) {
        try { state.passkeyWindow.close(); } catch (_) { /* best effort */ }
      }
      state.passkeyWindow = null;
      render();
    }
  }

  async function runWithConcurrency(items, limit, worker) {
    let nextIndex = 0;
    const runNext = async () => {
      while (nextIndex < items.length) {
        const item = items[nextIndex];
        nextIndex += 1;
        await worker(item);
      }
    };
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, runNext));
  }

  async function confirmLogoutSessions() {
    if (state.logoutPending) return;
    const ids = state.pendingLogoutJobIds.filter((id) => isLogoutEligible(state.jobs.get(id)));
    if (!ids.length) {
      $('logout-sessions-confirm').close();
      toast('Không còn tài khoản đủ điều kiện logout.', 'error');
      return;
    }
    const button = $('logout-sessions-confirm-action');
    state.logoutPending = true;
    ids.forEach((id) => {
      state.selectedLogoutJobIds.delete(id);
      const job = state.jobs.get(id);
      if (job) state.jobs.set(id, { ...job, sessions_logging_out: true });
    });
    $('logout-sessions-cancel').disabled = true;
    button.disabled = true;
    button.innerHTML = `${icon('loader-2')}<span>Đang logout 0/${ids.length}…</span>`;
    render();
    let completed = 0;
    let succeeded = 0;
    const concurrency = Math.max(1, Math.min(Number(state.settings['twofa.max_concurrent']) || 1, ids.length));
    await runWithConcurrency(ids, concurrency, async (id) => {
      try {
        const data = await api(`/api/jobs/${encodeURIComponent(id)}/logout-sessions`, {
          method: 'POST', body: JSON.stringify({ confirm: 'LOGOUT_ALL_SESSIONS' }),
        });
        if (state.jobs.has(id)) state.jobs.set(id, data.job);
        succeeded += 1;
      } catch (_) {
        // Never retry automatically: a lost response may follow successful revocation.
      } finally {
        const latest = state.jobs.get(id);
        if (latest) state.jobs.set(id, { ...latest, sessions_logging_out: false });
        completed += 1;
        button.innerHTML = `${icon('loader-2')}<span>Đang logout ${completed}/${ids.length}…</span>`;
        render();
      }
    });

    state.logoutPending = false;
    button.disabled = false;
    $('logout-sessions-cancel').disabled = false;
    $('logout-sessions-confirm').close();
    render();
    if (succeeded === ids.length) {
      toast(`Đã gửi logout ${succeeded} tài khoản. Các phiên có thể mất tối đa 30 phút để đăng xuất.`);
    } else {
      toast(`Đã gửi logout ${succeeded}/${ids.length} tài khoản; ${ids.length - succeeded} tài khoản lỗi. Tool không tự động thử lại.`, 'error');
    }
  }

  function openDeleteChatsConfirmation(id) {
    const job = state.jobs.get(id);
    if (!job || job.status !== 'success' || job.account_state !== 'live' || job.chat_deleting || isLoggingOut(job)) {
      throw new Error('Tài khoản chưa đủ điều kiện xóa dữ liệu chat.');
    }
    state.pendingDeleteChatsJobId = id;
    $('delete-chats-email').textContent = job.email;
    $('delete-chats-confirm').showModal();
  }

  async function confirmDeleteChats() {
    const id = state.pendingDeleteChatsJobId;
    const current = id ? state.jobs.get(id) : null;
    if (!id || !current || current.chat_deleting) return;
    const button = $('delete-chats-confirm-action');
    state.jobs.set(id, { ...current, chat_deleting: true });
    button.disabled = true;
    button.innerHTML = `${icon('loader-2')}<span>Đang xóa dữ liệu…</span>`;
    render();
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/delete-chats`, {
        method: 'POST',
        body: JSON.stringify({ confirm: 'DELETE_ALL_CHATS' }),
      });
      state.jobs.set(id, data.job);
      state.pendingDeleteChatsJobId = null;
      $('delete-chats-confirm').close();
      toast('Đã xóa hết dữ liệu chat của tài khoản.');
    } catch (error) {
      const latest = state.jobs.get(id);
      if (latest) state.jobs.set(id, { ...latest, chat_deleting: false });
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.innerHTML = `${icon('trash-x')}<span>Xác nhận xóa hết chat</span>`;
      render();
    }
  }

  function openRowChangeConfirmation(id) {
    const job = state.jobs.get(id);
    if (!job || !isRowChangeEligible(job)) {
      throw new Error('Tài khoản chưa đủ điều kiện đổi 2FA.');
    }
    const usageView = window.UsageUI?.buildUsageView(job.usage);
    state.pendingChangeJobId = id;
    $('row-change-email').textContent = job.email;
    $('row-change-plan').textContent = planLabel(job);
    $('row-change-usage').textContent = usageView?.remainingLabel || 'Chưa có Usage';
    $('row-change-confirm').showModal();
  }

  async function confirmRowChange() {
    const id = state.pendingChangeJobId;
    if (!id) return;
    state.pendingChangeJobId = null;
    $('row-change-confirm').close();
    const button = $('row-change-confirm-action');
    button.disabled = true;
    state.pendingRowChangeResults.add(id);
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/change-2fa`, { method: 'POST' });
      state.jobs.set(id, data.job);
      render();
      toast('Đã đưa tài khoản vào hàng đợi đổi 2FA.');
    } catch (error) {
      state.pendingRowChangeResults.delete(id);
      toast(`Không thể đổi 2FA: ${error.message}`, 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function copyRaw(id) {
    const raw = await api(`/api/jobs/${encodeURIComponent(id)}/raw`, { cache: 'no-store' });
    await writeClipboard(raw);
    toast('Đã copy raw data của tài khoản.');
  }

  function clearRowChangeResult() {
    state.activeChangeResultJobId = null;
    $('row-change-result-email').textContent = '';
    $('row-change-result-raw').value = '';
    $('row-change-result-status').textContent = 'Đang tải dữ liệu đã xác minh…';
    $('row-change-result-copy').disabled = true;
  }

  function closeRowChangeResult() {
    const modal = $('row-change-result');
    if (modal.open) modal.close();
    else clearRowChangeResult();
  }

  async function openRowChangeResult(id) {
    const job = state.jobs.get(id);
    if (!job) return;
    const modal = $('row-change-result');
    clearRowChangeResult();
    state.activeChangeResultJobId = id;
    $('row-change-result-email').textContent = job.email;
    if (!modal.open) modal.showModal();
    try {
      const raw = await api(`/api/jobs/${encodeURIComponent(id)}/raw`, { cache: 'no-store' });
      if (state.activeChangeResultJobId !== id || !modal.open) return;
      $('row-change-result-raw').value = raw;
      $('row-change-result-status').textContent = 'Chỉ hiển thị trong modal này và sẽ được xóa khi đóng.';
      $('row-change-result-copy').disabled = false;
    } catch (_) {
      if (state.activeChangeResultJobId !== id || !modal.open) return;
      $('row-change-result-status').textContent = 'Chưa tải được dữ liệu mới. Có thể dùng nút Copy raw trên dòng để thử lại.';
      toast('Đổi 2FA thành công nhưng chưa tải được dữ liệu mới.', 'error');
    }
  }

  async function copyRowChangeResult() {
    const raw = $('row-change-result-raw').value;
    if (!raw) throw new Error('Dữ liệu mới chưa sẵn sàng.');
    await writeClipboard(raw);
    toast('Đã copy dữ liệu mới sau khi đổi 2FA.');
  }

  function clearTwoFAHistory() {
    state.twofaHistory = [];
    $('twofa-history-count').textContent = '0';
    $('copy-twofa-history').disabled = true;
    $('export-twofa-history').disabled = true;
    $('twofa-history-list').replaceChildren();
  }

  function renderTwoFAHistory() {
    const list = $('twofa-history-list');
    list.replaceChildren();
    $('twofa-history-count').textContent = String(state.twofaHistory.length);
    $('copy-twofa-history').disabled = state.twofaHistory.length === 0;
    $('export-twofa-history').disabled = state.twofaHistory.length === 0;
    if (!state.twofaHistory.length) {
      const empty = document.createElement('div');
      empty.className = 'history-state';
      empty.textContent = 'Chưa có tài khoản đổi 2FA thành công.';
      list.appendChild(empty);
      return;
    }
    state.twofaHistory.forEach((entry, index) => {
      const row = document.createElement('article');
      row.className = 'history-item';
      const sequence = document.createElement('span');
      sequence.className = 'history-sequence';
      sequence.textContent = String(index + 1).padStart(2, '0');
      const content = document.createElement('div');
      content.className = 'history-content';
      const meta = document.createElement('div');
      meta.className = 'history-meta';
      const email = document.createElement('strong');
      email.textContent = entry.email;
      const changedAt = document.createElement('time');
      const date = new Date(Number(entry.changed_at) * 1000);
      changedAt.textContent = Number.isNaN(date.getTime()) ? 'Không rõ thời gian' : date.toLocaleString('vi-VN');
      meta.append(email, changedAt);
      const raw = document.createElement('code');
      raw.textContent = entry.raw;
      content.append(meta, raw);
      const copy = document.createElement('button');
      copy.className = 'history-copy';
      copy.type = 'button';
      copy.dataset.historyIndex = String(index);
      copy.innerHTML = `${icon('copy')}<span>Copy</span>`;
      copy.setAttribute('aria-label', `Copy bản ghi ${index + 1}`);
      row.append(sequence, content, copy);
      list.appendChild(row);
    });
  }

  async function openTwoFAHistory() {
    const modal = $('twofa-history');
    clearTwoFAHistory();
    const loading = document.createElement('div');
    loading.className = 'history-state';
    loading.textContent = 'Đang tải lịch sử bảo mật…';
    $('twofa-history-list').appendChild(loading);
    modal.showModal();
    try {
      const data = await api('/api/twofa-history', { cache: 'no-store' });
      if (!modal.open) return;
      state.twofaHistory = Array.isArray(data.history) ? data.history : [];
      renderTwoFAHistory();
    } catch (error) {
      if (!modal.open) return;
      clearTwoFAHistory();
      const failed = document.createElement('div');
      failed.className = 'history-state error';
      failed.textContent = 'Không tải được lịch sử. Hãy đóng và thử lại.';
      $('twofa-history-list').appendChild(failed);
      toast(`Không tải được lịch sử 2FA: ${error.message}`, 'error');
    }
  }

  function closeTwoFAHistory() {
    const modal = $('twofa-history');
    if (modal.open) modal.close();
    else clearTwoFAHistory();
  }

  async function copyTwoFAHistory(index = null) {
    const raw = index === null
      ? state.twofaHistory.map((entry) => entry.raw).join('\n')
      : state.twofaHistory[index]?.raw;
    if (!raw) throw new Error('Không có dữ liệu lịch sử để copy.');
    await writeClipboard(raw);
    toast(index === null ? 'Đã copy toàn bộ lịch sử 2FA.' : 'Đã copy bản ghi lịch sử.');
  }

  function exportTwoFAHistory() {
    const raw = state.twofaHistory.map((entry) => entry.raw).join('\n');
    if (!raw) throw new Error('Không có dữ liệu lịch sử để xuất.');
    const blob = new Blob([`${raw}\n`], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'twofa-history.txt';
    anchor.hidden = true;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    toast(`Đã xuất ${state.twofaHistory.length} bản ghi lịch sử.`);
  }

  async function openLogs(id) {
    const job = state.jobs.get(id);
    if (!job) return;
    $('detail-title').textContent = job.email;
    $('detail-status').innerHTML = `<span class="status ${job.status}">${escapeHtml(statusLabel(job))}</span>`;
    $('detail-logs').textContent = 'Đang tải log...';
    openDrawer('detail-drawer');
    try {
      const data = await api(`/api/jobs/${id}/logs`);
      $('detail-logs').textContent = data.logs.join('\n') || 'Chưa có log.';
      $('detail-logs').scrollTop = $('detail-logs').scrollHeight;
    } catch (error) { $('detail-logs').textContent = error.message; }
  }

  function openDrawer(id) {
    closeDrawers();
    $(id).classList.add('open'); $(id).setAttribute('aria-hidden', 'false');
    $('drawer-backdrop').classList.add('open');
  }

  function closeDrawers() {
    document.querySelectorAll('.drawer').forEach((drawer) => { drawer.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); });
    $('drawer-backdrop').classList.remove('open');
  }

  function loadSettingsForm() {
    const concurrency = state.settings['twofa.max_concurrent'];
    $('setting-concurrency').value = concurrency;
    $('quick-concurrency').value = concurrency;
    $('setting-timeout').value = state.settings['twofa.job_timeout'];
    $('setting-auto-retry').checked = state.settings['twofa.auto_retry'];
    $('setting-retry-max').value = state.settings['twofa.auto_retry_max'];
    $('setting-retry-delay').value = state.settings['twofa.auto_retry_delay'];
    $('change-enabled').checked = Boolean(state.settings['twofa.change_enabled']);
    $('read-usage-enabled').checked = state.settings['twofa.read_usage'] !== false;
    $('read-payment-enabled').checked = state.settings['twofa.read_payment_methods'] !== false;
    renderMode();
    renderInspectionOptions();
  }

  function settingsPayload(maxConcurrent = state.settings['twofa.max_concurrent']) {
    return {
      max_concurrent: Number(maxConcurrent),
      job_timeout: Number(state.settings['twofa.job_timeout']),
      auto_retry: Boolean(state.settings['twofa.auto_retry']),
      auto_retry_max: Number(state.settings['twofa.auto_retry_max']),
      auto_retry_delay: Number(state.settings['twofa.auto_retry_delay']),
      change_enabled: Boolean(state.settings['twofa.change_enabled']),
      read_usage: Boolean(state.settings['twofa.read_usage']),
      read_payment_methods: Boolean(state.settings['twofa.read_payment_methods']),
      input_draft: $('combo-input').value,
    };
  }

  async function saveQuickConcurrency() {
    const input = $('quick-concurrency');
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < 1 || value > 10) {
      input.value = state.settings['twofa.max_concurrent'];
      throw new Error('Số luồng phải từ 1 đến 10.');
    }
    if (value === Number(state.settings['twofa.max_concurrent'])) return;
    const data = await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(settingsPayload(value)),
      timeoutMs: LAUNCH_REQUEST_TIMEOUT_MS,
    });
    state.settings = data.settings;
    loadSettingsForm();
    toast(`Đã đổi sang ${value} luồng chạy đồng thời.`);
  }

  async function retryFailed() {
    const failed = [...state.jobs.values()].filter((job) => ['error', 'cancelled'].includes(job.status) && job.retryable !== false);
    if (!failed.length) return;
    const button = $('retry-failed');
    button.disabled = true;
    let retried = 0;
    try {
      for (const job of failed) {
        try {
          const data = await api(`/api/jobs/${job.id}/retry`, { method: 'POST' });
          state.jobs.set(job.id, data.job);
          retried += 1;
          render();
        } catch (error) {
          toast(`${job.email}: ${error.message}`, 'error');
        }
      }
      toast(`Đã đưa ${retried}/${failed.length} tài khoản lỗi vào chạy lại.`);
    } finally {
      render();
    }
  }

  function openClearFailedConfirmation() {
    const clearable = [...state.jobs.values()].filter(isClearableFailure);
    if (!clearable.length) return;
    const jobs = [...state.jobs.values()];
    $('clear-confirm-count').textContent = clearable.length;
    $('clear-confirm-live').textContent = jobs.filter((job) => job.account_state === 'live').length;
    $('clear-confirm-active').textContent = jobs.filter((job) => ['queued', 'running'].includes(job.status)).length;
    $('clear-failed-confirm-action').innerHTML = `${icon('trash-x')}<span>Dọn ${clearable.length} tài khoản lỗi</span>`;
    $('clear-failed-confirm').showModal();
  }

  async function clearFailedAccounts() {
    const clearable = [...state.jobs.values()].filter(isClearableFailure);
    if (!clearable.length) {
      $('clear-failed-confirm').close();
      return;
    }
    $('clear-failed-confirm').close();
    const button = $('clear-failed');
    const confirmButton = $('clear-failed-confirm-action');
    button.disabled = true;
    confirmButton.disabled = true;
    try {
      const data = await api('/api/jobs/failed', { method: 'DELETE' });
      state.jobs.clear();
      data.jobs.forEach((job) => state.jobs.set(job.id, job));
      state.pendingRechecks.forEach((id) => {
        if (!state.jobs.has(id)) state.pendingRechecks.delete(id);
      });
      render();
      toast(`Đã dọn ${data.deleted} tài khoản lỗi. Tài khoản Live được giữ lại.`);
    } catch (error) {
      toast(`Không dọn được tài khoản lỗi: ${error.message}`, 'error');
    } finally {
      confirmButton.disabled = false;
      render();
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    try {
      const payload = {
        max_concurrent: Number($('setting-concurrency').value),
        job_timeout: Number($('setting-timeout').value),
        auto_retry: $('setting-auto-retry').checked,
        auto_retry_max: Number($('setting-retry-max').value),
        auto_retry_delay: Number($('setting-retry-delay').value),
        change_enabled: Boolean(state.settings['twofa.change_enabled']),
        read_usage: Boolean(state.settings['twofa.read_usage']),
        read_payment_methods: Boolean(state.settings['twofa.read_payment_methods']),
        input_draft: $('combo-input').value,
      };
      const data = await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
      state.settings = data.settings;
      loadSettingsForm();
      closeDrawers(); toast('Đã lưu cấu hình runtime vào SQLite.');
    } catch (error) { toast(error.message, 'error'); }
  }

  async function exportOutput() {
    try {
      const response = await fetch('/api/output', { headers: { 'X-Auth-Token': state.token } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'twofa-success.txt'; anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) { toast(error.message, 'error'); }
  }

  async function exportFiltered() {
    const meta = window.UsageUI?.buildFilteredExportMeta(state.filter, filteredJobs().length);
    if (!meta || meta.count === 0) return;
    const button = $('export-filtered');
    button.disabled = true;
    try {
      const response = await fetch(`/api/jobs/export?view=${encodeURIComponent(meta.view)}`, {
        headers: { 'X-Auth-Token': state.token },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const serverFileName = /filename="?([A-Za-z0-9._-]+)"?/i.exec(disposition)?.[1];
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = serverFileName || meta.fileName;
      anchor.hidden = true;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      const exportedCountRaw = response.headers.get('x-export-count');
      const exportedCount = exportedCountRaw === null ? Number.NaN : Number(exportedCountRaw);
      toast(`Đã xuất ${Number.isInteger(exportedCount) ? exportedCount : meta.count} tài khoản theo bộ lọc hiện tại.`);
    } catch (error) {
      toast(`Không xuất được dữ liệu: ${error.message}`, 'error');
    } finally {
      render();
    }
  }

  function flushRealtimeEvents(events) {
    let needsRender = false;
    let needsOutputRefresh = false;
    const successfulRowChanges = [];

    events.forEach((payload) => {
      if (payload.type === 'snapshot') {
        const previousJobs = state.jobs;
        const nextJobs = new Map();
        payload.jobs.forEach((job) => {
          if (window.RealtimeUI.shouldRefreshOutput({ type: 'job', job }, previousJobs.get(job.id))) {
            needsOutputRefresh = true;
          }
          nextJobs.set(job.id, job);
        });
        state.pendingRechecks.forEach((id) => {
          if (!nextJobs.has(id)) state.pendingRechecks.delete(id);
        });
        state.jobs = nextJobs;
        state.workerHealth = payload.worker_health || state.workerHealth;
        needsRender = true;
      } else if (payload.type === 'job') {
        const previous = state.jobs.get(payload.job.id);
        if (window.RealtimeUI.shouldRefreshOutput(payload, previous)) needsOutputRefresh = true;
        const rowChangeSucceeded = (
          state.pendingRowChangeResults.has(payload.job.id)
          && payload.job.mode === 'change_2fa'
          && payload.job.status === 'success'
          && previous?.status !== 'success'
        );
        state.jobs.set(payload.job.id, payload.job);
        needsRender = true;
        if (rowChangeSucceeded) {
          state.pendingRowChangeResults.delete(payload.job.id);
          successfulRowChanges.push(payload.job.id);
        }
      } else if (payload.type === 'worker_health') state.workerHealth = payload.worker_health;
      else if (payload.type === 'removed') {
        state.jobs.delete(payload.id);
        state.selectedLogoutJobIds.delete(payload.id);
        state.pendingRowChangeResults.delete(payload.id);
        state.pendingRechecks.delete(payload.id);
        needsRender = true;
      }
    });

    renderConnection();
    if (needsRender) render();
    if (needsOutputRefresh) refreshOutput();
    successfulRowChanges.forEach((id) => {
      toast('Đổi 2FA thành công. Dữ liệu mới đã được đưa vào Verified Output.');
      openRowChangeResult(id);
    });
  }

  function disconnectEvents() {
    state.events?.close();
    state.events = null;
    state.eventBatcher?.clear();
    state.eventBatcher = null;
  }

  function connectEvents() {
    if (!state.token || document.visibilityState === 'hidden' || state.events) return;
    state.eventBatcher = window.RealtimeUI.createEventBatcher({
      delay: 100,
      flush: flushRealtimeEvents,
    });
    state.events = new EventSource(`/api/events?token=${encodeURIComponent(state.token)}`);
    state.events.onopen = () => { state.connection = 'online'; renderConnection(); };
    state.events.onerror = () => { state.connection = 'reconnecting'; renderConnection(); };
    state.events.onmessage = ({ data }) => {
      try {
        state.eventBatcher.push(JSON.parse(data));
      } catch (error) {
        console.warn('Bỏ qua realtime event không hợp lệ.', error);
      }
    };
  }

  function syncEventVisibility() {
    if (document.visibilityState === 'hidden') {
      disconnectEvents();
      return;
    }
    connectEvents();
  }

  async function init() {
    try {
      const data = await fetch('/api/bootstrap').then((response) => response.json());
      state.token = data.token; state.settings = data.settings; state.workerHealth = data.worker_health; state.connection = 'online';
      data.jobs.forEach((job) => state.jobs.set(job.id, job));
      const hasActiveJobs = data.jobs.some((job) => ['queued', 'running'].includes(job.status));
      $('combo-input').value = hasActiveJobs ? String(state.settings['twofa.input_draft'] || '') : '';
      loadSettingsForm(); updateEditor(); scrollEditorToTop(true); renderConnection(); render(); renderOutput(); syncEventVisibility(); await refreshOutput();
    } catch (_) { state.connection = 'offline'; renderConnection(); toast('Không kết nối được localhost :5033', 'error'); }
  }

  $('combo-input').addEventListener('input', scheduleDraftSave);
  $('combo-input').addEventListener('paste', handleComboPaste);
  $('combo-input').addEventListener('scroll', syncEditorScroll);
  $('success-output').addEventListener('scroll', syncOutputScroll);
  $('jump-input-top').addEventListener('click', () => {
    $('combo-input').focus({ preventScroll: true });
    scrollEditorToTop(true);
  });
  $('change-enabled').addEventListener('change', async () => {
    const previous = Boolean(state.settings['twofa.change_enabled']);
    const next = $('change-enabled').checked;
    state.settings['twofa.change_enabled'] = next;
    renderMode();
    try {
      const data = await api('/api/settings', { method: 'PUT', body: JSON.stringify(settingsPayload()) });
      state.settings = data.settings;
    } catch (error) {
      state.settings['twofa.change_enabled'] = previous;
      $('change-enabled').checked = previous;
      renderMode();
      toast(error.message, 'error');
    }
  });
  $('read-usage-enabled').addEventListener('change', () => {
    saveInspectionOption('read-usage-enabled', 'twofa.read_usage', 'Đọc Usage');
  });
  $('read-payment-enabled').addEventListener('change', () => {
    saveInspectionOption('read-payment-enabled', 'twofa.read_payment_methods', 'Đọc Payment');
  });
  $('quick-concurrency').addEventListener('change', async () => {
    try { await saveQuickConcurrency(); } catch (error) { toast(error.message, 'error'); }
  });
  $('launch-batch').addEventListener('click', async () => {
    try { await saveQuickConcurrency(); openLaunchConfirmation(); } catch (error) { toast(error.message, 'error'); }
  });
  $('confirm-launch').addEventListener('click', launch);
  $('cancel-launch').addEventListener('click', () => { state.pendingLaunch = null; $('launch-confirm').close(); });
  $('retry-failed').addEventListener('click', retryFailed);
  $('clear-failed').addEventListener('click', openClearFailedConfirmation);
  $('clear-failed-confirm-action').addEventListener('click', clearFailedAccounts);
  $('clear-failed-cancel').addEventListener('click', () => $('clear-failed-confirm').close());
  $('row-change-confirm-action').addEventListener('click', confirmRowChange);
  $('row-change-cancel').addEventListener('click', () => { state.pendingChangeJobId = null; $('row-change-confirm').close(); });
  $('delete-chats-confirm-action').addEventListener('click', confirmDeleteChats);
  $('logout-sessions-confirm-action').addEventListener('click', confirmLogoutSessions);
  $('passkey-confirm-action').addEventListener('click', confirmPasskey);
  $('passkey-cancel').addEventListener('click', () => { if (!state.passkeyPreparing) $('passkey-confirm').close(); });
  $('passkey-confirm').addEventListener('cancel', (event) => { if (state.passkeyPreparing) event.preventDefault(); });
  $('passkey-confirm').addEventListener('close', () => {
    state.pendingPasskeyJobId = null;
    const launch = $('passkey-open');
    launch.hidden = true;
    launch.removeAttribute('href');
    $('passkey-email').textContent = '';
    $('passkey-note').textContent = '';
  });
  $('logout-sessions-cancel').addEventListener('click', () => {
    if (!state.logoutPending) $('logout-sessions-confirm').close();
  });
  $('logout-sessions-confirm').addEventListener('cancel', (event) => {
    if (state.logoutPending) event.preventDefault();
  });
  $('logout-sessions-confirm').addEventListener('close', () => {
    state.pendingLogoutJobIds = [];
    $('logout-sessions-email').textContent = '';
  });
  $('delete-chats-cancel').addEventListener('click', () => {
    const job = state.jobs.get(state.pendingDeleteChatsJobId);
    if (job?.chat_deleting) return;
    state.pendingDeleteChatsJobId = null;
    $('delete-chats-confirm').close();
  });
  $('delete-chats-confirm').addEventListener('cancel', (event) => {
    const job = state.jobs.get(state.pendingDeleteChatsJobId);
    if (job?.chat_deleting) event.preventDefault();
    else state.pendingDeleteChatsJobId = null;
  });
  $('row-change-result-close').addEventListener('click', closeRowChangeResult);
  $('row-change-result-copy').addEventListener('click', async () => {
    try { await copyRowChangeResult(); } catch (error) { toast(error.message, 'error'); }
  });
  $('row-change-result').addEventListener('close', clearRowChangeResult);
  $('open-twofa-history').addEventListener('click', openTwoFAHistory);
  $('close-twofa-history').addEventListener('click', closeTwoFAHistory);
  $('twofa-history').addEventListener('close', clearTwoFAHistory);
  $('copy-twofa-history').addEventListener('click', async () => {
    try { await copyTwoFAHistory(); } catch (error) { toast(error.message, 'error'); }
  });
  $('export-twofa-history').addEventListener('click', () => {
    try { exportTwoFAHistory(); } catch (error) { toast(error.message, 'error'); }
  });
  $('twofa-history-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-history-index]');
    if (!button) return;
    try { await copyTwoFAHistory(Number(button.dataset.historyIndex)); }
    catch (error) { toast(error.message, 'error'); }
  });
  $('job-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]'); const row = event.target.closest('tr');
    if (button && row) jobAction(row.dataset.id, button.dataset.action);
  });
  document.querySelectorAll('.filter').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('.filter').forEach((item) => item.classList.remove('active'));
    button.classList.add('active'); state.filter = button.dataset.filter; render();
  }));
  document.querySelectorAll('[data-target]').forEach((button) => button.addEventListener('click', () => $(button.dataset.target).scrollIntoView({ behavior: 'smooth' })));
  $('open-settings').addEventListener('click', () => { loadSettingsForm(); openDrawer('settings-drawer'); });
  $('close-detail').addEventListener('click', closeDrawers); $('close-settings').addEventListener('click', closeDrawers); $('drawer-backdrop').addEventListener('click', closeDrawers);
  $('settings-form').addEventListener('submit', saveSettings);
  $('copy-output').addEventListener('click', copyOutput);
  $('export-output').addEventListener('click', exportOutput);
  $('export-filtered').addEventListener('click', exportFiltered);
  $('logout-selected').addEventListener('click', () => {
    try { openSelectedLogoutConfirmation(); } catch (error) { toast(error.message, 'error'); }
  });
  $('select-all-logout').addEventListener('change', (event) => {
    const visibleJobs = filteredJobs();
    const eligibleIds = visibleJobs.filter(isLogoutEligible).map((job) => job.id);
    if (event.target.checked) eligibleIds.forEach((id) => state.selectedLogoutJobIds.add(id));
    else eligibleIds.forEach((id) => state.selectedLogoutJobIds.delete(id));
    render();
  });
  $('job-list').addEventListener('change', (event) => {
    const checkbox = event.target.closest('[data-logout-select]');
    const row = event.target.closest('tr');
    if (!checkbox || !row) return;
    const id = row.dataset.id;
    if (checkbox.checked && isLogoutEligible(state.jobs.get(id))) state.selectedLogoutJobIds.add(id);
    else state.selectedLogoutJobIds.delete(id);
    render();
  });
  $('stop-all').addEventListener('click', async () => { try { await api('/api/jobs/stop-all', { method: 'POST' }); toast('Đã gửi lệnh dừng toàn bộ.'); } catch (error) { toast(error.message, 'error'); } });
  async function clearAllJobs() {
    if (state.clearAllPending) return;
    state.clearAllPending = true;
    render();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
      const data = await api('/api/jobs', { method: 'DELETE', signal: controller.signal });
      state.jobs.clear();
      state.selectedLogoutJobIds.clear();
      state.pendingRowChangeResults.clear();
      state.pendingRechecks.clear();
      render();
      await refreshOutput();
      toast(`Đã dọn ${data.deleted} tài khoản.`);
    } catch (error) {
      toast(error.name === 'AbortError' ? 'Dọn danh sách quá lâu, hãy kiểm tra lại trạng thái rồi thử lại.' : error.message, 'error');
    } finally {
      clearTimeout(timeout);
      state.clearAllPending = false;
      render();
    }
  }
  $('clear-all').addEventListener('click', clearAllJobs);
  document.addEventListener('visibilitychange', syncEventVisibility);
  window.addEventListener('pagehide', disconnectEvents);
  updateEditor(); renderOutput(); init();
})();
