(function passwordHistoryUiModule(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.PasswordHistoryUI = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  'use strict';

  function filterEntries(entries, query) {
    const normalized = String(query || '').trim().toLocaleLowerCase('vi');
    return (Array.isArray(entries) ? entries : [])
      .map((entry, index) => ({ entry, index }))
      .filter(({ entry }) => !normalized || String(entry?.email || '').toLocaleLowerCase('vi').includes(normalized));
  }

  function credentialParts(entry) {
    const pieces = String(entry?.raw || '').split('|');
    return {
      email: String(entry?.email || pieces[0] || ''),
      password: pieces[1] || '',
      secret: pieces.slice(2).join('|'),
    };
  }

  function rawLines(filteredEntries) {
    return (Array.isArray(filteredEntries) ? filteredEntries : [])
      .map(({ entry }) => String(entry?.raw || ''))
      .filter(Boolean);
  }

  return Object.freeze({ filterEntries, credentialParts, rawLines });
}));
