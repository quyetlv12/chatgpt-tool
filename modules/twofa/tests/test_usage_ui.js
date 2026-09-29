'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const UsageUI = require('../change 2fa community/static/usage-ui.js');

const summary = UsageUI.buildUsageView({
  used_percent: 28,
  remaining_percent: 72,
  reset_after_seconds: 529489,
  allowed: true,
  limit_reached: false,
  credits: { has_credits: false, unlimited: false, balance: '0' },
  reset_credits: { available_count: 3 },
});

assert.deepEqual(summary, {
  used: 28,
  usedLabel: '28%',
  remainingLabel: '72% còn lại',
  statusLabel: 'Reset sau 6 ngày 3 giờ',
  creditLabel: null,
  resetCreditLabel: '3 lượt reset',
  tone: 'healthy',
});

const limited = UsageUI.buildUsageView({
  used_percent: 100,
  remaining_percent: 0,
  reset_after_seconds: 30,
  allowed: false,
  limit_reached: true,
  credits: { has_credits: true, unlimited: false, balance: '12.5' },
});

assert.equal(limited.tone, 'limited');
assert.equal(limited.statusLabel, 'Đã chạm giới hạn');
assert.equal(limited.creditLabel, 'Credit 12.5');
assert.equal(limited.resetCreditLabel, null);
assert.equal(UsageUI.buildUsageView({
  used_percent: 0,
  reset_credits: { available_count: 0 },
}).resetCreditLabel, '0 lượt reset');
assert.equal(UsageUI.buildUsageView({
  used_percent: 0,
  reset_credits: { available_count: true },
}).resetCreditLabel, null);
assert.equal(UsageUI.buildUsageView(null), null);
assert.equal(UsageUI.buildUsageView({ used_percent: 101 }), null);
assert.equal(UsageUI.isBelowUsageThreshold({ used_percent: 0 }, 50), true);
assert.equal(UsageUI.isBelowUsageThreshold({ used_percent: 49.99 }, 50), true);
assert.equal(UsageUI.isBelowUsageThreshold({ used_percent: 50 }, 50), false);
assert.equal(UsageUI.isBelowUsageThreshold({ used_percent: 72 }, 50), false);
assert.equal(UsageUI.isBelowUsageThreshold(null, 50), false);
assert.equal(UsageUI.isUsageFullyUsed({ used_percent: 100 }), true);
assert.equal(UsageUI.isUsageFullyUsed({ used_percent: 99.99 }), false);
assert.equal(UsageUI.isUsageFullyUsed({ used_percent: true }), false);
assert.equal(UsageUI.isUsageFullyUsed(null), false);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'free', status: 'success', account_state: 'live', usage: null,
}), true);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'FREE', status: 'success', account_state: 'live', usage: { used_percent: 101 },
}), true);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'free', status: 'success', account_state: 'live', usage: { used_percent: 0 },
}), false);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'plus', status: 'success', account_state: 'live', usage: null,
}), true);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'plus', status: 'success', account_state: 'live', usage: null, usage_enabled: false,
}), false);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'PLUS', status: 'success', account_state: 'live', usage: { used_percent: 42 },
}), false);
assert.equal(UsageUI.isFreeWithoutUsage({
  plan: 'free', status: 'error', account_state: 'live', usage: null,
}), false);
assert.deepEqual(UsageUI.buildFilteredExportMeta('usage-low', 2), {
  view: 'usage-low',
  count: 2,
  label: 'Xuất Usage < 50% (2)',
  fileName: 'twofa-usage-under-50.txt',
});
assert.deepEqual(UsageUI.buildFilteredExportMeta('usage-full', 3), {
  view: 'usage-full',
  count: 3,
  label: 'Xuất Usage = 100% (3)',
  fileName: 'twofa-usage-100.txt',
});
assert.deepEqual(UsageUI.buildFilteredExportMeta('free-no-usage', 4), {
  view: 'free-no-usage',
  count: 4,
  label: 'Xuất Free + Plus · Chưa có Usage (4)',
  fileName: 'twofa-free-plus-no-usage.txt',
});
assert.equal(UsageUI.buildFilteredExportMeta('unknown', 2), null);

const browserSandbox = {};
const source = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/usage-ui.js'), 'utf8');
vm.runInNewContext(source, browserSandbox);
assert.equal(typeof browserSandbox.UsageUI.buildUsageView, 'function');

const appSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/app.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/index.html'), 'utf8');
const dashboardSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/dashboard.css'), 'utf8');
const faviconPath = path.join(__dirname, '../change 2fa community/static/favicon.svg');
const iconSpritePath = path.join(__dirname, '../change 2fa community/static/tabler-icons.svg');
const iconSprite = fs.readFileSync(iconSpritePath, 'utf8');
assert.match(htmlSource, /rel="icon" type="image\/svg\+xml" href="\/assets\/favicon\.svg"/);
assert.doesNotMatch(htmlSource, /fonts\.googleapis\.com|fonts\.gstatic\.com/);
assert.match(dashboardSource, /--font-ui:"Avenir Next"/);
assert.match(dashboardSource, /--font-mono:"SFMono-Regular"/);
assert.match(dashboardSource, /\.section-head h2\{font-family:var\(--font-display\)/);
assert.doesNotMatch(htmlSource, /<p>Đổi khóa TOTP theo lô[^<]*<\/p>/);
assert.doesNotMatch(htmlSource, /Đổi 2FA nhanh|Giữ nguyên mật khẩu|Nhập combo bên trái/);
assert.doesNotMatch(htmlSource, /class="overview-strip"/);
assert.match(htmlSource, /<header class="topbar compact-topbar">[\s\S]*?class="metric-row topbar-metrics"[\s\S]*?<\/header>/);
assert.equal(fs.existsSync(faviconPath), true);
assert.equal(fs.existsSync(iconSpritePath), true);
assert.match(iconSprite, /id="ti-settings"/);
assert.match(iconSprite, /id="ti-player-play"/);
assert.match(iconSprite, /id="ti-copy"/);
assert.match(iconSprite, /id="ti-download"/);
assert.match(htmlSource, /\/assets\/tabler-icons\.svg\?v=1\.0\.1#ti-history/);
assert.match(htmlSource, /\/assets\/tabler-icons\.svg\?v=1\.0\.1#ti-settings/);
assert.match(appSource, /const icon = \(name\)/);
assert.match(appSource, /icon\('refresh'\)/);
assert.match(appSource, /view\.resetCreditLabel/);
assert.match(appSource, /usage-reset-credit/);
assert.match(htmlSource, /usage-ui\.js\?v=1\.3\.9/);
assert.match(htmlSource, /app\.js\?v=1\.3\.35/);
assert.match(htmlSource, /dashboard\.css\?v=1\.5\.16/);
assert.match(appSource, /icon\('list-details'\)/);
assert.match(appSource, /job\.status === 'queued'/);
assert.match(appSource, /Chờ worker…/);
assert.match(appSource, /\? 'ĐÃ CHECK LIVE'/);
assert.doesNotMatch(appSource, /ĐÃ CHECK LIVE · KHÔNG ĐỔI 2FA/);
assert.match(appSource, /WORKER DEGRADED/);
assert.match(appSource, /function renderConnection\(\)[\s\S]*?if \(!label\) return;/);
assert.match(htmlSource, /id="filter-running"[^>]*>[\s\S]*?<span>Đang xử lý<\/span>/);
assert.match(htmlSource, /id="filter-usage-full"[^>]*data-filter="usage-full"[^>]*>[\s\S]*?<span>Usage = 100%<\/span>[\s\S]*?id="count-usage-full"/);
assert.match(htmlSource, /id="filter-free-no-usage"[^>]*data-filter="free-no-usage"[^>]*>[\s\S]*?<span>Free \+ Plus · Chưa có Usage<\/span>[\s\S]*?id="count-free-no-usage"/);
assert.match(appSource, /isUsageFullyUsed\(job\.usage\)/);
assert.match(appSource, /state\.filter === 'usage-full'/);
assert.match(appSource, /isFreeWithoutUsage\(job\)/);
assert.match(appSource, /state\.filter === 'free-no-usage'/);
assert.match(appSource, /job\.usage_refreshing/);
assert.match(appSource, /data-action="refresh-usage"/);
assert.match(appSource, /Đọc lại Usage/);
assert.match(appSource, /\/refresh-usage/);
assert.match(appSource, /await refreshUsage\(id\)/);
assert.match(appSource, /pendingRechecks: new Set\(\)/);
assert.match(appSource, /data-action="recheck"/);
assert.match(appSource, /Check lại tài khoản/);
assert.match(appSource, /\/recheck`/);
assert.match(appSource, /state\.pendingRechecks\.has\(id\)/);
assert.match(appSource, /state\.pendingRechecks\.add\(id\)/);
assert.match(appSource, /state\.pendingRechecks\.delete\(id\)/);
assert.match(appSource, /data-action="passkey"/);
assert.doesNotMatch(htmlSource, /id="action-tooltip"/);
assert.match(iconSprite, /id="ti-fingerprint"/);
assert.doesNotMatch(htmlSource, /<th>Retry<\/th>/);
assert.doesNotMatch(appSource, /<td>\$\{job\.retry_count\}<\/td>/);
assert.match(htmlSource, /id="row-change-result"/);
assert.match(htmlSource, /id="row-change-result-raw"[^>]*readonly/);
assert.match(htmlSource, /id="row-change-result-copy"/);
assert.match(appSource, /pendingRowChangeResults: new Set\(\)/);
assert.match(appSource, /state\.pendingRowChangeResults\.add\(id\)/);
assert.match(appSource, /state\.pendingRowChangeResults\.has\(payload\.job\.id\)/);
assert.match(appSource, /successfulRowChanges\.push\(payload\.job\.id\)/);
assert.match(appSource, /openRowChangeResult\(id\)/);
assert.match(appSource, /clearAllPending: false/);
assert.match(appSource, /async function clearAllJobs\(\)/);
assert.match(appSource, /if \(state\.clearAllPending\) return;/);
assert.match(appSource, /state\.clearAllPending = true/);
assert.match(appSource, /state\.clearAllPending = false/);
assert.match(appSource, /clear-all'\)\.addEventListener\('click', clearAllJobs\)/);
assert.match(appSource, /\/raw`, \{ cache: 'no-store' \}/);
assert.match(appSource, /await writeClipboard\(raw\)/);
assert.match(appSource, /\$\('row-change-result-raw'\)\.value = ''/);
assert.match(htmlSource, /id="open-twofa-history"/);
assert.match(htmlSource, /id="twofa-history"/);
assert.match(htmlSource, /id="twofa-history-list"/);
assert.match(appSource, /api\('\/api\/twofa-history', \{ cache: 'no-store' \}\)/);
assert.match(appSource, /state\.twofaHistory = \[\]/);
assert.match(appSource, /copyTwoFAHistory/);
assert.match(htmlSource, /\/assets\/realtime-ui\.js\?v=/);
assert.match(htmlSource, /app\.js\?v=1\.3\.35/);
assert.ok(
  htmlSource.indexOf('/assets/realtime-ui.js') < htmlSource.indexOf('/assets/app.js'),
  'realtime helper must load before app.js',
);
assert.match(appSource, /RealtimeUI\.createEventBatcher/);
assert.doesNotMatch(
  appSource,
  /state\.events\.onmessage[\s\S]*?refreshOutput\(\);[\s\S]*?\n\s*};/,
  'the SSE message handler must not refresh output for every event',
);

console.log('usage-ui: formatter and browser export passed');
