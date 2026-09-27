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

console.log('launch UI timeout contract passed');
