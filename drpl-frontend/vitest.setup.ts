/**
 * jsdom implements neither `matchMedia` nor `scrollTo`. ThemeProvider calls the
 * first on mount and throws without it, which takes the whole tree down and
 * makes every assertion fail with an unrelated-looking "unable to find
 * element". Stub them once here rather than in each test.
 */
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {} as typeof Element.prototype.scrollTo
}
