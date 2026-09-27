'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'change 2fa community/static/app.js'), 'utf8');
const suiteHtml = fs.readFileSync(path.join(root, '../../web/index.html'), 'utf8');

assert.match(suiteHtml, /<iframe[^>]+allow="clipboard-write"/);
assert.match(appSource, /async function writeClipboard\(text\)/);
assert.match(appSource, /document\.execCommand\('copy'\)/);
assert.match(appSource, /async function copyRaw[\s\S]*?await writeClipboard\(raw\)/);

console.log('clipboard UI fallback passed');
