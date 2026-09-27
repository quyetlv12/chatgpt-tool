'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'change 2fa community/static/app.js'), 'utf8');
const passwordSource = fs.readFileSync(path.join(root, 'change 2fa community/static/password-ui.js'), 'utf8');
const passwordInitStart = passwordSource.indexOf('  async function init()');
const passwordInitEnd = passwordSource.indexOf('  function closeWorkspace()', passwordInitStart);
const passwordInitSource = passwordSource.slice(passwordInitStart, passwordInitEnd);

// Chromium permits only a small HTTP/1.1 connection pool per origin. A page
// must not keep both SSE streams alive in every background tab, otherwise
// several open tool tabs can consume the pool and make reload wait forever.
assert.match(appSource, /function disconnectEvents\(\)[\s\S]*?state\.events\?\.close\(\)[\s\S]*?state\.events = null/);
assert.match(appSource, /function syncEventVisibility\(\)[\s\S]*?document\.visibilityState === 'hidden'[\s\S]*?disconnectEvents\(\)/);
assert.match(appSource, /document\.addEventListener\('visibilitychange', syncEventVisibility\)/);
assert.match(appSource, /window\.addEventListener\('pagehide', disconnectEvents\)/);

assert.match(passwordSource, /function disconnectEvents\(\)[\s\S]*?state\.events\?\.close\(\)[\s\S]*?state\.events = null/);
assert.match(passwordSource, /function shouldConnectEvents\(\)[\s\S]*?document\.visibilityState !== 'hidden'[\s\S]*?password-workspace'\)\.open/);
assert.match(passwordSource, /function syncEventVisibility\(\)[\s\S]*?shouldConnectEvents\(\)[\s\S]*?disconnectEvents\(\)/);
assert.match(passwordSource, /async function openWorkspace\(\)[\s\S]*?showModal\(\)[\s\S]*?syncEventVisibility\(\)/);
assert.ok(passwordInitStart >= 0 && passwordInitEnd > passwordInitStart);
assert.doesNotMatch(passwordInitSource, /connectEvents\(\)/, 'password SSE must stay lazy until the password workspace is open');
assert.match(passwordSource, /function closeWorkspace\(\)[\s\S]*?disconnectEvents\(\)[\s\S]*?clearWorkspaceCredentials\(\)/);
assert.match(passwordSource, /password-workspace'\)\.addEventListener\('close', closeWorkspace\)/);
assert.match(passwordSource, /document\.addEventListener\('visibilitychange', syncEventVisibility\)/);
assert.match(passwordSource, /window\.addEventListener\('pagehide', disconnectEvents\)/);

console.log('event connection lifecycle contract passed');
