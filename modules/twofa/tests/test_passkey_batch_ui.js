const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', 'change 2fa community', 'static');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'passkey-ui.js'), 'utf8');

for (const id of [
  'open-passkey-tool', 'passkey-workspace', 'passkey-combo-input',
  'launch-passkey-jobs', 'passkey-job-list', 'passkey-logs',
]) assert.match(html, new RegExp(`id="${id}"`));
assert.match(html, /passkey-ui\.js\?v=1\.0\.0/);
assert.match(app, /\/api\/passkey\/jobs/);
assert.match(app, /LAUNCH_PASSKEY/);
assert.match(app, /window\.open\('about:blank', '_blank'\)/);
assert.match(app, /handoff_ready/);
assert.match(app, /Đã mở tab passkey/);
assert.doesNotMatch(app, /credentials\.create|privateKey/);
console.log('passkey batch UI: workspace, concurrent handoff tabs, fallback links and key privacy passed');
