(function (root, factory) {
  const api = factory();
  root.UsageUI = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function validPercent(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
  }

  function formatPercent(value) {
    return `${Number(value.toFixed(2))}%`;
  }

  function formatResetDuration(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null;
    const totalSeconds = Math.floor(seconds);
    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    if (days) return `${days} ngày${hours ? ` ${hours} giờ` : ''}`;
    if (hours) return `${hours} giờ${minutes ? ` ${minutes} phút` : ''}`;
    if (minutes) return `${minutes} phút`;
    return 'dưới 1 phút';
  }

  function creditLabel(credits) {
    if (!credits || typeof credits !== 'object') return null;
    if (credits.unlimited === true) return 'Credit không giới hạn';
    if (credits.has_credits !== true) return null;
    const balance = ['string', 'number'].includes(typeof credits.balance)
      ? String(credits.balance).trim().slice(0, 64)
      : '';
    return balance ? `Credit ${balance}` : 'Có credit';
  }

  function buildUsageView(usage) {
    if (!usage || typeof usage !== 'object' || !validPercent(usage.used_percent)) return null;
    const used = Number(usage.used_percent.toFixed(2));
    const remaining = validPercent(usage.remaining_percent)
      ? Number(usage.remaining_percent.toFixed(2))
      : Number((100 - used).toFixed(2));
    const limited = usage.limit_reached === true || usage.allowed === false;
    const resetDuration = formatResetDuration(usage.reset_after_seconds);
    const resetCount = usage.reset_credits?.available_count;

    return {
      used,
      usedLabel: formatPercent(used),
      remainingLabel: `${formatPercent(remaining)} còn lại`,
      statusLabel: limited
        ? 'Đã chạm giới hạn'
        : resetDuration
          ? `Reset sau ${resetDuration}`
          : 'Chưa có giờ reset',
      creditLabel: creditLabel(usage.credits),
      resetCreditLabel: Number.isInteger(resetCount) && resetCount >= 0
        ? `${resetCount} lượt reset`
        : null,
      tone: limited ? 'limited' : used >= 80 ? 'warning' : 'healthy',
    };
  }

  function isBelowUsageThreshold(usage, threshold = 50) {
    if (!usage || typeof usage !== 'object' || !validPercent(usage.used_percent)) return false;
    if (typeof threshold !== 'number' || !Number.isFinite(threshold)) return false;
    return usage.used_percent < threshold;
  }

  function isUsageFullyUsed(usage) {
    return Boolean(
      usage
      && typeof usage === 'object'
      && validPercent(usage.used_percent)
      && usage.used_percent === 100
    );
  }

  function isFreeWithoutUsage(job) {
    const plan = String(job?.plan || '').toLowerCase();
    return Boolean(
      job
      && job.usage_enabled !== false
      && ['free', 'plus'].includes(plan)
      && job.status === 'success'
      && job.account_state === 'live'
      && (!job.usage || typeof job.usage !== 'object' || !validPercent(job.usage.used_percent))
    );
  }

  function buildFilteredExportMeta(view, count) {
    const definitions = {
      all: ['Tất cả', 'twofa-all.txt'],
      running: ['Đang chạy', 'twofa-running.txt'],
      success: ['Thành công', 'twofa-success-tab.txt'],
      'usage-low': ['Usage < 50%', 'twofa-usage-under-50.txt'],
      'usage-full': ['Usage = 100%', 'twofa-usage-100.txt'],
      'free-no-usage': ['Free + Plus · Chưa có Usage', 'twofa-free-plus-no-usage.txt'],
      error: ['Lỗi', 'twofa-errors.txt'],
    };
    const definition = definitions[view];
    if (!definition || !Number.isInteger(count) || count < 0) return null;
    return {
      view,
      count,
      label: `Xuất ${definition[0]} (${count})`,
      fileName: definition[1],
    };
  }

  return {
    buildUsageView,
    formatResetDuration,
    isBelowUsageThreshold,
    isUsageFullyUsed,
    isFreeWithoutUsage,
    buildFilteredExportMeta,
  };
}));
