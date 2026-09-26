// ============================================================
// DRPL Extension - IREPS detail-page title extraction (pure, no DOM/chrome deps)
// ============================================================
//
// The IREPS *listing* clips every tender title to 60 chars, and no attribute on
// the listing carries more. The full, untruncated title exists only on the
// tender detail (viewNIT) page, which renders label/value lines. This parses the
// title out of that page's text.
//
// Priority order matters: the specific "Tender Title" / "Name of Work" labels win
// over generic "description"/"subject" so we never mistake a blurb for the title.

import { cleanTitle } from './selectors';

// Ordered most-specific first. Each label is matched only at a line start (after
// a newline or string start) so a stray "...titled below..." in prose can't match.
const TITLE_LABELS = [
  'tender\\s*title',
  'name\\s*of\\s*work',
  'work\\s*description',
  'short\\s*description',
  'subject',
];

/**
 * Extract the full tender title from IREPS detail-page text (document.body.innerText).
 * Returns '' when no title label is present — callers should then keep whatever
 * title they already have rather than overwrite it with junk.
 */
export function pickTenderTitle(pageText: string): string {
  if (!pageText) return '';

  for (const label of TITLE_LABELS) {
    // Anchor the label to a line start; capture the rest of the line as the value.
    const re = new RegExp(`(?:^|\\n)\\s*${label}\\s*:?\\s*(.+)`, 'i');
    const m = pageText.match(re);
    if (!m) continue;

    let value = m[1];
    // If a following field label bled onto the same line (2+ spaces then
    // "Label:"), cut it off at that boundary.
    value = value.replace(/\s{2,}[A-Z][A-Za-z ]+\s*:.*$/, '');
    const cleaned = cleanTitle(value);
    if (cleaned) return cleaned;
  }

  return '';
}
