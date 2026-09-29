const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'change 2fa community/static/app.js'), 'utf8');

// Launch is a local control-plane request. If localhost stops responding, the
// UI must release the button and show a retryable error instead of spinning
// forever.
assert.match(source, /const LAUNCH_REQUEST_TIMEOUT_MS = \d+;/);
assert.match(source, /api\('\/api\/jobs',\s*\{[\s\S]*timeoutMs:\s*LAUNCH_REQUEST_TIMEOUT_MS/);
assert.match(source, /api\('\/api\/settings',\s*\{[\s\S]*timeoutMs:\s*LAUNCH_REQUEST_TIMEOUT_MS/);
assert.match(source, /error\?\.name === 'AbortError'/);
assert.match(source, /Khởi chạy quá thời gian chờ/);
assert.match(source, /const hasActiveJobs = data\.jobs\.some\(\(job\) => \['queued', 'running'\]\.includes\(job\.status\)\)/);
assert.match(source, /hasActiveJobs \? String\(state\.settings\['twofa\.input_draft'\] \|\| ''\) : ''/);

const vm = require('node:vm');
const restoreDraft = source.slice(source.indexOf('      const hasActiveJobs ='), source.indexOf('      loadSettingsForm(); updateEditor();'));
for (const [statuses, draft, expected] of [
  [[], '', ''],
  [[], 'synthetic draft', ''],
  [['success'], 'synthetic draft', ''],
  [['error', 'cancelled'], 'synthetic draft', ''],
  [['queued'], 'synthetic draft', 'synthetic draft'],
  [['success', 'running'], 'synthetic draft', 'synthetic draft'],
  [['running'], '', ''],
]) {
  const input = { value: '' };
  vm.runInNewContext(restoreDraft, {
    data: { jobs: statuses.map(status => ({ status })) },
    state: { settings: { 'twofa.input_draft': draft } },
    $: () => input,
  });
  assert.equal(input.value, expected);
}

console.log('launch UI timeout contract passed');
