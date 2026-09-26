// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { extractGemItemsTitle } from './gem-title';

/**
 * Fixtures mirror the real GeM `all-bids` card markup produced by the portal's
 * JS template (captured from https://bidplus.gem.gov.in/all-bids):
 *
 *   <div class="row"><strong>Items:</strong>&nbsp;
 *     if (category > 30 chars)  -> <a data-content="FULL" title="BOQ">FIRST30...</a>
 *     else                      -> FULL (BOQ)
 *
 * The visible <a> text is clipped to 30 chars + "..."; the FULL item text lives
 * in the anchor's data-content attribute. THAT is the tender title.
 */
function cardEl(inner: string): Element {
  const wrap = document.createElement('div');
  wrap.className = 'card';
  wrap.innerHTML = inner;
  return wrap;
}

const LONG_ITEM =
  'Custom Bid For Services - Providing of Skilled and Unskilled Manpower Services for Housekeeping';

describe('extractGemItemsTitle', () => {
  it('returns the FULL item text from data-content, not the 30-char clipped anchor text', () => {
    const card = cardEl(`
      <div class="card-body"><div class="row">
        <div class="col-md-4">
          <div class="row"><strong>Items:</strong>&nbsp;<a data-toggle="popover"
            title="Manpower BOQ" data-content="${LONG_ITEM}">Custom Bid For Services - Prov...</a></div>
          <div class="row"><strong>Quantity:</strong>&nbsp;1800</div>
        </div>
        <div class="col-md-5">
          <div class="row"><strong>Department Name And Address:</strong>&nbsp;</div>
          <div class="row">Ministry of DefenceDepartment of Military Affairs</div>
        </div>
      </div></div>`);

    expect(extractGemItemsTitle(card)).toBe(LONG_ITEM);
  });

  it('returns the plain text item when the category is short (no popover anchor)', () => {
    const card = cardEl(`
      <div class="card-body"><div class="row">
        <div class="col-md-4">
          <div class="row"><strong>Items:</strong>&nbsp;Ball Bearings (BOQ-77)</div>
          <div class="row"><strong>Quantity:</strong>&nbsp;50</div>
        </div>
      </div></div>`);

    expect(extractGemItemsTitle(card)).toBe('Ball Bearings (BOQ-77)');
  });

  it('never returns the Department Name field as the title', () => {
    const card = cardEl(`
      <div class="card-body"><div class="row">
        <div class="col-md-4">
          <div class="row"><strong>Items:</strong>&nbsp;<a data-content="${LONG_ITEM}">Custom...</a></div>
        </div>
        <div class="col-md-5">
          <div class="row"><strong>Department Name And Address:</strong>&nbsp;</div>
          <div class="row">Ministry of Defence</div>
        </div>
      </div></div>`);

    const title = extractGemItemsTitle(card);
    expect(title).not.toMatch(/Department Name/i);
    expect(title).toBe(LONG_ITEM);
  });

  it('returns empty string when there is no Items field', () => {
    const card = cardEl(`<div class="card-body"><div class="row">
      <div class="col-md-5"><div class="row"><strong>Quantity:</strong>&nbsp;5</div></div>
    </div></div>`);
    expect(extractGemItemsTitle(card)).toBe('');
  });
});
