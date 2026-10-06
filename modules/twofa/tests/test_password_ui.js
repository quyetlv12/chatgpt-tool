'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'change 2fa community/static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'change 2fa community/static/password-ui.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'change 2fa community/static/dashboard.css'), 'utf8');

assert.match(html, /id="open-password-tool"/);
assert.match(html, /id="setting-target-password" type="text"/);
assert.match(html, /id="password-workspace"/);
assert.match(html, /id="password-workspace-target-password" type="text"/);
assert.match(html, /id="save-workspace-target-password"/);
assert.match(html, /id="password-combo-input"/);
assert.match(html, /id="password-success-output" readonly/);
assert.match(html, /id="password-history-search" type="search"/);
assert.match(html, /id="password-history-total"/);
assert.match(html, /dashboard\.css\?v=1\.5\.24/);
assert.match(html, /class="password-history-title-cluster"/);
assert.match(html, /class="password-history-title-icon"/);
assert.match(html, /password-history-ui\.js\?v=1\.0\.0/);
assert.match(html, /password-ui\.js\?v=1\.2\.2/);

assert.match(script, /\/api\/password-settings/);
assert.match(script, /\/api\/password\/jobs/);
assert.match(script, /\/api\/password\/output/);
assert.match(script, /\/api\/password\/history/);
assert.match(script, /\/api\/password\/events\?token=/);
assert.match(script, /mutation_started: 'ĐÃ BẮT ĐẦU MUTATION'/);
assert.doesNotMatch(script, /mutation_started: 'ĐÃ GỬI MUTATION'/);

assert.match(script, /function clearWorkspaceCredentials\(\)[\s\S]*?state\.output = ''[\s\S]*?password-combo-input/);
assert.match(script, /function clearWorkspaceCredentials\(\)[\s\S]*?password-workspace-target-password'\)\.value = ''/);
assert.match(script, /password-workspace'\)\.addEventListener\('close', closeWorkspace\)/);
assert.match(script, /function clearHistory\(\)[\s\S]*?state\.history = \[\]/);
assert.match(script, /function renderHistory\(\)/);
assert.match(script, /PasswordHistoryUI\.filterEntries/);
assert.match(script, /password-history-search'\)\.addEventListener\('input', renderHistory\)/);
assert.match(script, /function clearHistory\(\)[\s\S]*?password-history-search'\)\.value = ''/);
assert.match(script, /password-history'\)\.addEventListener\('close', clearHistory\)/);
assert.match(script, /function clearTargetPasswordInput\(\)[\s\S]*?setting-target-password/);
assert.match(script, /api\('\/api\/password-settings\?reveal=true'\)/);
assert.match(script, /if \(reveal && \$\('settings-drawer'\)\.getAttribute\('aria-hidden'\) === 'false'\)/);
assert.match(script, /password-workspace-target-password'\)\.value = data\.target_password \|\| ''/);
assert.match(script, /save-workspace-target-password'\)\.addEventListener\('click'/);
assert.match(script, /password-logs'\)\.addEventListener\('close'/);
assert.match(script, /clearPending: false/);
assert.match(script, /async function clearJobs\(\)/);
assert.match(script, /if \(state\.clearPending\) return;/);
assert.match(script, /state\.clearPending = true/);
assert.match(script, /state\.clearPending = false/);
assert.match(script, /clear-password-jobs'\)\.addEventListener\('click', \(\) => clearJobs\(\)/);

assert.match(css, /\.password-workspace\{/);
assert.match(css, /\.password-target-editor\{/);
assert.match(css, /\.password-workbench\{display:grid;grid-template-columns:1fr 1fr/);
assert.match(css, /\.password-history-modal\{[\s\S]*?height:fit-content/);
assert.match(css, /\.password-history-modal\[open\]\{[\s\S]*?align-content:start/);
assert.match(css, /\.password-history-head\{height:76px;min-height:76px;display:grid;grid-template-columns:auto minmax\(0,1fr\) auto;align-items:center/);
assert.match(css, /\.password-history-head h2\{[\s\S]*?line-height:1\.1/);
assert.match(css, /\.password-history-toolbar\{[\s\S]*?height:58px[\s\S]*?padding:8px 20px/);
assert.match(css, /\.password-history-search\{/);
assert.match(css, /\.password-history-entry\{/);
assert.match(css, /@media\(max-width:760px\)[\s\S]*?\.password-workbench\{grid-template-columns:1fr/);
assert.match(css, /@media\(max-width:760px\)[\s\S]*?\.password-history-head\{height:82px;min-height:82px/);
assert.match(css, /@media\(max-width:760px\)[\s\S]*?\.password-history-toolbar\{height:94px;[\s\S]*?min-height:94px/);
assert.match(css, /@media\(max-width:760px\)[\s\S]*?\.password-history-head \.history-close\{grid-column:2;grid-row:1/);
assert.match(css, /@media\(max-width:760px\)[\s\S]*?\.password-history-search\{height:38px/);

console.log('password UI tests passed');
