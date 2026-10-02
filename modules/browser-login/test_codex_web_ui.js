const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const [, script] of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(script);
const screen = html.match(/<section[^>]*id="tab-codex-web"[\s\S]*?<\/section>/)[0];
assert.match(screen, /class="workflow-grid codex-workflow-grid"/);
assert.match(html, /\.codex-workflow-grid\s*\{\s*grid-template-columns:\s*minmax\(440px,\s*1\.35fr\)\s*minmax\(360px,\s*0\.9fr\)/);
assert.match(screen, /id="codex-web-accounts-input"/);
assert.doesNotMatch(screen, /oauth-accounts-input|web-login-reload/);
assert.match(html, /data-tab="codex-web">Login Codex/);
assert.doesNotMatch(screen, /codex-web-url|desktop-auth/);
assert.match(screen, /id="codex-web-workers-input"/);
const script = html.slice(html.indexOf('      // === Codex Web:'), html.indexOf('      // === Helpers ==='));
assert.doesNotMatch(script, /\/api\/oauth|\/api\/import|localStorage/);
function setup() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', style: {}, classList: {contains: () => true}, addEventListener(name, fn) { this[name] = fn; } });
    return nodes.get(id);
  };
  const calls = [], messages = [];
  const ctx = { $, API: '', AbortSignal, Number, URL, document: {hidden: false}, window: { confirm: () => false }, setInterval() {},
    normalizeAllLines: s => s, esc: s => String(s).replaceAll('<','&lt;'),
    showMsg: (_, s) => messages.push(s), hideMsg() {},
    fetch: async (url, options) => {
      calls.push({url, options});
      return {ok: true, json: async () => url.endsWith('/start') ? {runId:'new'} : {runId:'new',running:true,total:1,results:[],logs:[]}};
    } };
  vm.createContext(ctx); vm.runInContext(script, ctx);
  return {ctx, $, calls, messages};
}
(async () => {
  const {ctx,$,calls,messages} = setup();
  await $('btn-codex-web-login').click();
  assert.equal(calls.length,0); assert.match(messages.at(-1),/hợp lệ/);
  const accountsInput='demo@example.test|synthetic|\nsecond@example.test|synthetic|';
  $('codex-web-accounts-input').value=accountsInput;
  $('codex-web-workers-input').value='2';
  await $('btn-codex-web-login').click();
  assert.equal(calls[0].url,'/api/codex-web/start');
  assert.deepEqual(JSON.parse(calls[0].options.body),{accounts:['demo@example.test|synthetic|','second@example.test|synthetic|'],workers:2});
  assert.equal($('codex-web-accounts-input').value,accountsInput,'account input is preserved after starting');
  assert.equal($('btn-codex-web-login').disabled,true);
  const before=calls.length;
  await $('btn-codex-web-stop').click();
  assert.equal(calls.length,before,'cancelled confirmation sends no stop');
  ctx.fetch=async () => ({ok:true,json:async()=>({blockedBy:'chatgpt',results:[],logs:[]})});
  await vm.runInContext('refreshCodexWeb()',ctx);
  assert.equal($('btn-codex-web-login').disabled,true);
  assert.equal($('btn-codex-web-stop').style.display,'none');
  console.log('Codex UI: separate screen/API, validation, start, conflict and stop confirmation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
