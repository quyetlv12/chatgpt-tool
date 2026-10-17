const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
// Parse every inline script, then exercise the real reload controls without a browser.
for (const [, script] of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(script);
const toggleTag = html.match(/<input[^>]*id="web-login-reload-toggle"[^>]*>/)[0];
assert.doesNotMatch(toggleTag, /\bchecked\b/);
assert.match(html, /id="web-login-reload-seconds"[^>]*value="10"[^>]*required disabled/);
const dialogHtml = html.slice(html.indexOf('<dialog class="modal" id="web-login-reload-dialog"'), html.indexOf('</dialog>'));
assert.match(dialogHtml, /<form method="dialog">/);
assert.match(dialogHtml, /id="web-login-reload-toggle"/);
assert.match(dialogHtml, /type="button" value="cancel"/);
const pageStart = html.indexOf('id="tab-web-login"');
assert.ok(pageStart > 0);
assert.doesNotMatch(html.slice(pageStart, html.indexOf('</section>', pageStart)), /id="web-login-reload-toggle"/);
assert.match(dialogHtml, /id="web-login-reload-help">Mặc định tắt\. Chỉ áp dụng cho lần chạy này\.<\/div>/);
assert.doesNotMatch(dialogHtml, /web-login-reload-question|backend|wId/);
assert.doesNotMatch(html, /tab chính đã reload một lần/);
assert.match(html, /tabs_closed: 'đã dừng kiểm tra vì tab hoặc Chrome đã đóng'/);
const begin = html.indexOf('      function syncWebLoginReloadToggle()');
const end = html.indexOf('      function parseWebLoginAccounts()', begin);
const nodes = new Map([
  ['web-login-reload-toggle', { checked: false, addEventListener(_, handler) { this.change = handler; } }],
  ['web-login-reload-seconds', { value: '10' }],
  ['web-login-reload-toggle-text', {}],
  ['web-login-reload-dialog', {
    open: false,
    addEventListener(_, handler) { this.closed = handler; },
    showModal() { this.open = true; },
    close(value = '') { this.returnValue = value; this.open = false; this.closed(); },
  }],
]);
const context = { $: id => nodes.get(id) };
vm.createContext(context);
vm.runInContext(html.slice(begin, end), context);
const toggle = nodes.get('web-login-reload-toggle');
const input = nodes.get('web-login-reload-seconds');
assert.equal(input.disabled, true);
toggle.checked = true;
toggle.change();
assert.equal(input.disabled, false);
assert.equal(nodes.get('web-login-reload-toggle-text').textContent, 'Bật');
assert.match(html, /JSON\.stringify\(\{ accounts: accounts\.map\(account => account\.raw\), workers, browser, \.\.\.linkSettings, \.\.\.reloadSettings \}\)/);
assert.match(html, /const reloadSettings = await askWebLoginReloadSettings\(\);\s*if \(!reloadSettings\) return;/);
assert.ok(html.indexOf('const reloadSettings = await askWebLoginReloadSettings()') < html.indexOf("fetch(API + '/api/web-login/start'"));
(async () => {
  const dialog = nodes.get('web-login-reload-dialog');
  for (const value of ['1', '10', '37', '3600']) {
    const answer = context.askWebLoginReloadSettings();
    assert.equal(dialog.open, true);
    assert.equal(toggle.checked, false, 'each run starts with reload off');
    assert.equal(input.disabled, true);
    assert.equal(await context.askWebLoginReloadSettings(), null, 'duplicate click cannot start a second prompt');
    toggle.checked = true;
    toggle.change();
    input.value = value;
    dialog.close('start');
    assert.deepEqual(JSON.parse(JSON.stringify(await answer)), {reloadEnabled: true, reloadSeconds: Number(value)});
  }
  for (const action of ['cancel', '']) {
    const answer = context.askWebLoginReloadSettings();
    dialog.close(action);
    assert.equal(await answer, null, 'Cancel/Escape never starts login');
  }
  const answer = context.askWebLoginReloadSettings();
  dialog.close('start');
  assert.equal((await answer).reloadEnabled, false);
  console.log('Reload UI: run-time dialog, per-run default off, custom seconds, cancel/Escape, duplicate guard and payload passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
