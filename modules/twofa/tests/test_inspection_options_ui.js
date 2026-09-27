'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const appSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/app.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/index.html'), 'utf8');
const cssSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/dashboard.css'), 'utf8');

assert.match(htmlSource, /id="read-usage-enabled"[^>]*type="checkbox"/);
assert.match(htmlSource, /id="read-payment-enabled"[^>]*type="checkbox"/);
assert.match(htmlSource, />Đọc Usage</);
assert.match(htmlSource, />Đọc Payment</);
assert.match(appSource, /read_usage:\s*Boolean\(state\.settings\['twofa\.read_usage'\]\)/);
assert.match(appSource, /read_payment_methods:\s*Boolean\(state\.settings\['twofa\.read_payment_methods'\]\)/);
assert.match(appSource, /job\.usage_enabled === false[\s\S]*?Đã tắt/);
assert.match(appSource, /job\.payment_methods_enabled === false[\s\S]*?Đã tắt/);
assert.match(cssSource, /\.inspection-toggles/);
assert.match(cssSource, /\.inspection-toggle/);

console.log('inspection-options-ui: switches and disabled states passed');
