import { describe, it, expect } from 'vitest';
import { matchesScope, splitByScope } from './scope-matcher';
import type { TenderData, ScopeProfile } from './types';

const profile: ScopeProfile = {
  name: 'default',
  keyword_groups: [{ label: 'pumps', keywords: ['pump', 'motor'] }],
  exclusion_terms: ['scrap'],
  target_ministries: ['Railways'],
  value_min: 100000,
  value_max: 5000000,
  relevance_threshold: 0.6,
  is_active: true,
  updated_at: null,
};

function tender(over: Partial<TenderData>): TenderData {
  return {
    portal: 'ireps', tenderId: 'T1', title: 'Supply of pump sets',
    department: 'Railways', organisation: 'NR', estimatedValue: 500000,
    ...over,
  } as TenderData;
}

describe('matchesScope', () => {
  it('in-scope when keyword AND ministry AND value all match', () => {
    const m = matchesScope(tender({}), profile);
    expect(m.inScope).toBe(true);
    expect(m.missing).toEqual([]);
  });

  it('out when an exclusion term is present', () => {
    const m = matchesScope(tender({ title: 'pump scrap disposal' }), profile);
    expect(m.inScope).toBe(false);
  });

  it('out + missing:ministry when department/organisation do not match', () => {
    const m = matchesScope(tender({ department: 'PWD', organisation: 'State' }), profile);
    expect(m.inScope).toBe(false);
    expect(m.missing).toContain('ministry');
  });

  it('out + missing:value when estimatedValue is null', () => {
    const m = matchesScope(tender({ estimatedValue: null }), profile);
    expect(m.inScope).toBe(false);
    expect(m.missing).toContain('value');
  });

  it('out when no keyword matches', () => {
    const m = matchesScope(tender({ title: 'office stationery' }), profile);
    expect(m.inScope).toBe(false);
  });

  it('out when value is out of range', () => {
    const m = matchesScope(tender({ estimatedValue: 50000 }), profile);
    expect(m.inScope).toBe(false);
  });

  it('value passes with open-ended (null) bounds', () => {
    const open = { ...profile, value_min: null, value_max: null };
    const m = matchesScope(tender({ estimatedValue: 9_000_000 }), open);
    expect(m.inScope).toBe(true);
  });
});

describe('splitByScope', () => {
  it('caps capture, routes rest to overflow, non-matching to skipped', () => {
    const inScope = Array.from({ length: 12 }, (_, i) => tender({ tenderId: `IN-${i}` }));
    const out = tender({ tenderId: 'OUT', title: 'stationery' });
    const res = splitByScope([...inScope, out], profile, 10);
    expect(res.capture).toHaveLength(10);
    expect(res.overflow).toHaveLength(2);
    expect(res.skipped.map((t) => t.tenderId)).toContain('OUT');
  });
});
