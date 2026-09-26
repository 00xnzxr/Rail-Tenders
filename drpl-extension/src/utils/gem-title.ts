// ============================================================
// DRPL Extension - GeM title extraction (pure, DOM-only, no chrome deps)
// ============================================================
//
// On the GeM `all-bids` listing the tender's identifying text is the "Items:"
// field (the bid category name), NOT a "Tender Title" label — that label does
// not exist on this page. GeM also CLIPS the visible item text to 30 chars +
// "..." and stashes the full value in the anchor's `data-content` attribute:
//
//   <div class="row"><strong>Items:</strong>&nbsp;
//     <a data-toggle="popover" data-content="FULL ITEM TEXT">First 30 chars...</a>
//   </div>
//
// Reading textContent therefore yields a truncated title. This helper reads the
// full value from data-content, falling back to the plain row text for short
// items (which render without an anchor).

import { cleanTitle } from './selectors';

/**
 * Extract the full GeM tender title (the "Items:" category) from a listing card.
 * Prefers the untruncated `data-content` attribute over the clipped visible text.
 * Returns '' when the card has no Items field.
 */
export function extractGemItemsTitle(card: Element): string {
  // Locate the row whose leading <strong> label is "Items:".
  const strongs = Array.from(card.querySelectorAll('strong'));
  const itemsLabel = strongs.find((s) => /^\s*items\s*:?\s*$/i.test(s.textContent || ''));
  if (!itemsLabel) return '';

  const row = itemsLabel.parentElement;
  if (!row) return '';

  // Preferred: the full, untruncated item text in the popover anchor.
  const anchor = row.querySelector('a[data-content]');
  const dataContent = anchor?.getAttribute('data-content')?.trim();
  if (dataContent) return cleanTitle(dataContent);

  // Fallback (short items render as plain text, no anchor): row text minus label.
  const rowText = (row.textContent || '').replace(/ /g, ' ');
  const withoutLabel = rowText.replace(/^\s*items\s*:?\s*/i, '');
  return cleanTitle(withoutLabel);
}
