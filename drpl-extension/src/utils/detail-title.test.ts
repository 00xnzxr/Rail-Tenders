import { describe, it, expect } from 'vitest';
import { pickTenderTitle } from './detail-title';

/**
 * IREPS NIT detail pages render as label/value lines. In document.body.innerText
 * each field lands on its own line, e.g.:
 *
 *   Tender No : 6-S-O-KEMPPI-ASR-25-26A
 *   Tender Title : Comprehensive AMC of 102 Nos KEMPPI MIG Welding Plants and 10 Nos of Spot Welders
 *   Tender Value : ...
 *
 * The full (untruncated) title lives here — unlike the 60-char-capped listing.
 * pickTenderTitle pulls the labelled title out of that text.
 */

const FULL =
  'Comprehensive AMC of 102 Nos KEMPPI MIG Welding Plants and 10 Nos of Spot Welders at C&W Depot';

describe('pickTenderTitle', () => {
  it('extracts the full Tender Title (well over the 60-char listing cap)', () => {
    const text = [
      'Tender No : 6-S-O-KEMPPI-ASR-25-26A',
      `Tender Title : ${FULL}`,
      'Tender Value : 12,00,000',
    ].join('\n');
    const title = pickTenderTitle(text);
    expect(title).toBe(FULL);
    expect(title.length).toBeGreaterThan(60);
  });

  it('prefers the "Tender Title" / "Name of Work" label over a generic description', () => {
    const text = [
      'Short Description : short blurb',
      `Name of Work : ${FULL}`,
    ].join('\n');
    expect(pickTenderTitle(text)).toBe(FULL);
  });

  it('does not capture the value of an unrelated field that merely contains "title"', () => {
    const text = 'Please read the tender document titled below before bidding.\nTender Title : Real Title Here';
    expect(pickTenderTitle(text)).toBe('Real Title Here');
  });

  it('returns empty string when no title label is present (so the backend keeps the existing title)', () => {
    const text = 'Login-Indian Railways tenders for Goods, Works and Services\nUsername\nPassword';
    expect(pickTenderTitle(text)).toBe('');
  });

  it('trims a trailing next-field label if it bleeds onto the same line', () => {
    const text = 'Tender Title : One time repair of ACWP along with civil works   Tender Value : 5,00,000';
    expect(pickTenderTitle(text)).toBe('One time repair of ACWP along with civil works');
  });
});
