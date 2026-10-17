const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const [, script] of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(script);
assert.match(html, /<label[^>]*for="web-login-browser-select">Trình duyệt trên máy/);
assert.match(html, /cửa sổ ẩn danh riêng/);
const begin = html.indexOf('      async function loadWebLoginBrowsers()');
const end = html.indexOf('      function syncWebLoginReloadToggle()', begin);
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {value: '', textContent: '',
    addEventListener(_, fn) {this.change = fn;}, replaceChildren(...children) {this.options = children;}});
  return nodes.get(id);
};
const storage = new Map();
const context = vm.createContext({$, API: '', webBrowsersReady: false, WEB_LOGIN_BROWSER_KEY: 'browser',
  Option: function(name, id) {this.text = name; this.value = id;},
  localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
  fetch: async () => ({ok: true, json: async () => ({browsers: [{id:'auto',name:'Auto'}, {id:'brave',name:'Brave'}, {id:'chrome',name:'Chrome'}]})}),
});
(async () => {
  vm.runInContext(html.slice(begin, end), context);
  await context.loadWebLoginBrowsers();
  assert.equal($('web-login-browser-select').value, 'brave');
  assert.equal($('web-login-browser-select').disabled, false);
  $('web-login-browser-select').value = 'chrome';
  $('web-login-browser-select').change();
  await context.loadWebLoginBrowsers();
  assert.equal($('web-login-browser-select').value, 'chrome', 'saved choice is retained');
  context.fetch = async () => {throw new Error('offline');};
  await context.loadWebLoginBrowsers();
  assert.equal(context.webBrowsersReady, false);
  assert.equal($('web-login-browser-select').disabled, true);
  assert.match($('web-login-browser-help').textContent, /Không tải được/);
  assert.match(html, /if \(!webBrowsersReady\)/);
  assert.match(html, /workers, browser, \.\.\.linkSettings/);
  console.log('Browser UI: Brave default, persisted selection, loading/error guard and request payload passed');
})().catch(error => {console.error(error);process.exitCode = 1;});
