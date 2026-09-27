'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');

const historyUi = require(path.join(
  __dirname,
  '..',
  'change 2fa community',
  'static',
  'password-history-ui.js',
));

const entries = [
  { email: 'alpha@example.com', raw: 'alpha@example.com|Target-password-1!|TOTP-ALPHA' },
  { email: 'billing.team@example.com', raw: 'billing.team@example.com|Target-password-2!|TOTP-BRAVO' },
  { email: 'support@example.net', raw: 'support@example.net|Target-password-3!|TOTP-CHARLIE' },
];

assert.deepEqual(
  historyUi.filterEntries(entries, '  BILLING.TEAM  ').map((item) => item.index),
  [1],
  'search should trim and match email case-insensitively',
);
assert.deepEqual(
  historyUi.filterEntries(entries, '').map((item) => item.index),
  [0, 1, 2],
  'empty search should show every record in original order',
);
assert.deepEqual(
  historyUi.filterEntries(entries, 'missing'),
  [],
  'unknown email should produce an empty result',
);
assert.deepEqual(
  historyUi.credentialParts(entries[0]),
  {
    email: 'alpha@example.com',
    password: 'Target-password-1!',
    secret: 'TOTP-ALPHA',
  },
  'history cards should split the verified line into labeled fields',
);
assert.deepEqual(
  historyUi.rawLines(historyUi.filterEntries(entries, 'example.com')),
  [entries[0].raw, entries[1].raw],
  'copy should use only the currently visible search results',
);

console.log('password history UI tests passed');
