import { describe, it, expect } from 'vitest';
import { parseAggregatorTokens } from './aggregators';

describe('parseAggregatorTokens', () => {
  it('extracts location, bid type, and source portal from a TenderTiger row', () => {
    const text = 'TID:97729103 Railways Transport Services Saran, Bihar, India GeM NCB ' +
      'Tender Invited For Goods Transportation Worth :INR 1.00 Cr EMD :INR 2.00 Lac Due Date :14 July 2026';
    const t = parseAggregatorTokens(text);
    expect(t.location).toBe('Saran, Bihar, India');
    expect(t.bidType).toBe('NCB');
    expect(t.sourcePortal).toBe('gem');
  });

  it('returns undefined fields when tokens are absent', () => {
    const t = parseAggregatorTokens('TID:1 some tender with no portal tokens');
    expect(t.bidType).toBeUndefined();
    expect(t.sourcePortal).toBeUndefined();
  });

  it('normalizes IREPS as source portal', () => {
    const t = parseAggregatorTokens('TID:2 Pune, Maharashtra, India IREPS GCB widget');
    expect(t.sourcePortal).toBe('ireps');
    expect(t.bidType).toBe('GCB');
  });
});
