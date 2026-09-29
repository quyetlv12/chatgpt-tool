const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
// Parse every inline script, then exercise the real reload controls without a browser.
for (const [, script] of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(script);
const toggleTag = html.match(/<input[^>]*id="web-login-reload-toggle"[^>]*>/)[0];
assert.doesNotMatch(toggleTag, /\bchecked\b/);
assert.match(html, /id="web-login-reload-seconds"[^>]*value="10"[^>]*disabled/);
const begin = html.indexOf('      function syncWebLoginReloadToggle()');
const end = html.indexOf('      function parseWebLoginAccounts()', begin);
const nodes = new Map([
  ['web-login-reload-toggle', { checked: false, addEventListener(_, handler) { this.change = handler; } }],
  ['web-login-reload-seconds', { value: '10' }],
  ['web-login-reload-toggle-text', {}],
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
const settingsStart = html.indexOf('        const reloadSettings =');
const settingsEnd = html.indexOf('        if (linkSettings.openLinkEnabled', settingsStart);
const readSettings = vm.runInContext(`(function () { ${html.slice(settingsStart, settingsEnd)} return reloadSettings; })`, context);
context.showMsg = () => {};
for (const value of ['1', '10', '37', '3600']) {
  input.value = value;
  assert.equal(readSettings().reloadSeconds, Number(value));
}
for (const value of ['', '0', '-1', '1.5', '3601', 'bad']) {
  input.value = value;
  assert.equal(readSettings(), undefined);
}
toggle.checked = false;
toggle.change();
assert.equal(readSettings().reloadEnabled, false);
assert.equal(input.disabled, true);
assert.match(html, /JSON\.stringify\(\{ accounts: accounts\.map\(account => account\.raw\), workers, \.\.\.linkSettings, \.\.\.reloadSettings \}\)/);
console.log('Reload UI: default off, toggle, default/custom seconds, validation and payload passed');
