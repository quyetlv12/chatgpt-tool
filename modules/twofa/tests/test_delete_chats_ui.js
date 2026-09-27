'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'change 2fa community/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'change 2fa community/static/index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'change 2fa community/static/dashboard.css'), 'utf8');

assert.match(app, /job\.status === 'success'\s*&& job\.account_state === 'live'[\s\S]*?data-action="delete-chats"/);
assert.match(app, /\/api\/jobs\/\$\{encodeURIComponent\(id\)\}\/delete-chats/);
assert.match(app, /JSON\.stringify\(\{ confirm: 'DELETE_ALL_CHATS' \}\)/);
assert.match(app, /job\.chat_deleting/);
assert.match(html, /id="delete-chats-confirm"[^>]*aria-labelledby="delete-chats-title"/);
assert.match(html, /bao gồm cả chat trong Projects/i);
assert.match(html, /không thể hoàn tác/i);
assert.match(html, /Library[^<]*không bị xóa/i);
assert.match(css, /\.icon-button\.delete-chats/);
assert.match(css, /\.confirm-accent\.is-delete-chats/);

console.log('delete-chats-ui: eligible action, confirmation, pending state and warnings passed');
