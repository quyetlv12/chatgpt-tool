(function (root, factory) {
  const api = factory();
  root.PaymentUI = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const TYPE_LABELS = {
    card: 'Thẻ',
    paypal: 'PayPal',
    apple_pay: 'Apple Pay',
    google_pay: 'Google Pay',
    link: 'Link',
  };

  function safeLabel(value, maxLength = 32) {
    if (typeof value !== 'string') return null;
    const label = value.trim().toLowerCase();
    if (!label || label.length > maxLength || !/^[a-z0-9][a-z0-9 _-]*$/.test(label)) return null;
    return label;
  }

  function safeLast4(value) {
    if (!['string', 'number'].includes(typeof value)) return null;
    const last4 = String(value).trim();
    return /^\d{4}$/.test(last4) ? last4 : null;
  }

  function safeInteger(value, min, max) {
    if (!Number.isInteger(value) || value < min || value > max) return null;
    return value;
  }

  function buildMethodView(method) {
    if (!method || typeof method !== 'object') return null;
    const type = safeLabel(method.type);
    const brand = safeLabel(method.brand, 24);
    const last4 = safeLast4(method.last4);
    const month = safeInteger(method.exp_month, 1, 12);
    const year = safeInteger(method.exp_year, 2000, 9999);
    if (!type && !brand && !last4) return null;
    const numberLabel = last4 ? `•••• ${last4}` : null;
    const expiryLabel = month && year ? `${String(month).padStart(2, '0')}/${String(year).slice(-2)}` : null;
    return {
      typeLabel: TYPE_LABELS[type] || (type ? type.replaceAll('_', ' ').toUpperCase() : 'Thanh toán'),
      brandLabel: brand ? brand.replaceAll('_', ' ').toUpperCase() : null,
      numberLabel,
      expiryLabel,
      detailLabel: [numberLabel, expiryLabel ? `HSD ${expiryLabel}` : null].filter(Boolean).join(' · ') || null,
      isDefault: method.is_default === true,
    };
  }

  function paymentDateLabel(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const [year, month, day] = value.split('-').map(Number);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day) return null;
    return `Thanh toán ${String(day).padStart(2, '0')}/${String(month).padStart(2, '0')}/${year}`;
  }

  function buildPaymentView(paymentMethods, billingDate) {
    if (!Array.isArray(paymentMethods)) return null;
    const normalized = paymentMethods.map(buildMethodView).filter(Boolean).slice(0, 8);
    if (!normalized.length) return { empty: true, methods: [], extraCount: 0 };
    return {
      empty: false,
      methods: normalized.slice(0, 2),
      extraCount: Math.max(0, normalized.length - 2),
      paymentDateLabel: paymentDateLabel(billingDate),
    };
  }

  return { buildPaymentView };
}));
