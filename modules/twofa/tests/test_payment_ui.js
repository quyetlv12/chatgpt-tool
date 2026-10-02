'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const PaymentUI = require('../change 2fa community/static/payment-ui.js');

assert.deepEqual(PaymentUI.buildPaymentView([{
  type: 'card', brand: 'visa', last4: '4242', exp_month: 12, exp_year: 2030, is_default: true,
}], '2026-09-01'), {
  empty: false,
  methods: [{
    typeLabel: 'Thẻ',
    brandLabel: 'VISA',
    numberLabel: '•••• 4242',
    expiryLabel: '12/30',
    detailLabel: '•••• 4242 · HSD 12/30',
    isDefault: true,
  }],
  extraCount: 0,
  paymentDateLabel: 'Thanh toán 01/09/2026',
});

assert.deepEqual(PaymentUI.buildPaymentView([{ type: 'upi' }]), {
  empty: false,
  methods: [{
    typeLabel: 'UPI',
    brandLabel: null,
    numberLabel: null,
    expiryLabel: null,
    detailLabel: null,
    isDefault: false,
  }],
  extraCount: 0,
  paymentDateLabel: null,
});

assert.equal(PaymentUI.buildPaymentView([{ type: 'upi' }], 'not-a-date').paymentDateLabel, null);

assert.deepEqual(PaymentUI.buildPaymentView([]), { empty: true, methods: [], extraCount: 0 });
assert.equal(PaymentUI.buildPaymentView(null), null);
assert.equal(PaymentUI.buildPaymentView('bad'), null);

const source = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/payment-ui.js'), 'utf8');
const browserSandbox = {};
vm.runInNewContext(source, browserSandbox);
assert.equal(typeof browserSandbox.PaymentUI.buildPaymentView, 'function');

const appSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/app.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/index.html'), 'utf8');
const cssSource = fs.readFileSync(path.join(__dirname, '../change 2fa community/static/dashboard.css'), 'utf8');

assert.match(htmlSource, /<th>Thanh toán<\/th>/);
assert.match(htmlSource, /id="output-line-numbers" class="line-numbers"/);
assert.match(htmlSource, /\/assets\/payment-ui\.js\?v=/);
assert.ok(
  htmlSource.indexOf('/assets/payment-ui.js') < htmlSource.indexOf('/assets/app.js'),
  'payment helper must load before app.js',
);
assert.match(appSource, /function paymentCell\(job\)/);
assert.match(appSource, /output-line-numbers/);
assert.match(appSource, /function syncOutputScroll\(\)/);
assert.match(appSource, /PaymentUI\?\.buildPaymentView\(job\.payment_methods, job\.billing_date\)/);
assert.match(appSource, /class="payment-cell"/);
assert.match(appSource, /method\.detailLabel\s*\?/);
assert.match(appSource, /view\.paymentDateLabel/);
assert.match(appSource, /class="payment-date"/);
assert.doesNotMatch(appSource, /\.join\(' · '\) \|\| method\.typeLabel/);
assert.match(appSource, /Không có phương thức/);
assert.match(appSource, /Không đọc được/);
assert.match(cssSource, /\.payment-card/);
assert.match(cssSource, /\.output-wrap\{display:grid;grid-template-columns:46px minmax\(0,1fr\)/);
assert.match(cssSource, /\.workbench-grid\{grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)\}/);
assert.match(cssSource, /\.payment-placeholder/);
assert.match(
  cssSource,
  /\.table-wrap th:nth-child\(4\),\.table-wrap td:nth-child\(4\),\.table-wrap th:nth-child\(6\),\.table-wrap td:nth-child\(6\)\{width:1%;white-space:nowrap\}/,
  'account-check and payment columns should shrink-wrap their visible content',
);
assert.match(
  cssSource,
  /\.table-wrap th:nth-child\(5\)\{width:auto\}/,
  'weekly Usage should absorb the remaining table width',
);
assert.match(cssSource, /\.payment-cell\{min-width:0\}/);
assert.match(cssSource, /\.payment-card\{[^}]*width:max-content;[^}]*max-width:220px/);
assert.doesNotMatch(cssSource, /\.table-wrap th:nth-child\(3\)\{width:\d+%\}/);
assert.doesNotMatch(cssSource, /\.table-wrap th:nth-child\(5\)\{width:\d+%\}/);

console.log('payment-ui: safe payment rendering passed');
