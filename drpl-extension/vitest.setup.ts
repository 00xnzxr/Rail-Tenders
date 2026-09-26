// Minimal chrome extension API stub so content scripts (which register
// chrome.runtime listeners at module scope) can be imported under vitest/jsdom
// without throwing `ReferenceError: chrome is not defined`.
(globalThis as any).chrome = {
  runtime: {
    onMessage: { addListener: () => {} },
    sendMessage: () => {},
  },
};
