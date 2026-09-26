import type { TenderData, ScopeProfile } from './types';

export interface ScopeMatch {
  inScope: boolean;
  reasons: string[];
  missing: string[];
}

function norm(s: string | null | undefined): string {
  return (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

/** Strict scope gate: keyword AND ministry AND value must all pass.
 * Missing/unparseable fields fail their check and are listed in `missing`.
 * Exclusion terms apply regardless of other fields. */
export function matchesScope(tender: TenderData, profile: ScopeProfile): ScopeMatch {
  const reasons: string[] = [];
  const missing: string[] = [];

  const haystack = [norm(tender.title), norm(tender.department), norm(tender.category)]
    .filter(Boolean)
    .join(' | ');

  // Exclusion first — an excluded tender is never in scope.
  const excluded = (profile.exclusion_terms || []).some((t) => t && haystack.includes(norm(t)));
  if (excluded) {
    return { inScope: false, reasons: ['excluded'], missing };
  }

  // 1. Keyword
  let keywordOk = false;
  if (!haystack) {
    missing.push('keyword');
  } else {
    for (const g of profile.keyword_groups || []) {
      const hit = (g.keywords || []).find((k) => k && haystack.includes(norm(k)));
      if (hit) { keywordOk = true; reasons.push(`keyword:${hit}`); break; }
    }
    if (!keywordOk) missing.push('keyword');
  }

  // 2. Ministry
  const ministryHay = [norm(tender.department), norm(tender.organisation), norm(tender.sourcePortal)]
    .filter(Boolean)
    .join(' | ');
  let ministryOk = false;
  if (!ministryHay) {
    missing.push('ministry');
  } else {
    const hit = (profile.target_ministries || []).find((m) => m && ministryHay.includes(norm(m)));
    if (hit) { ministryOk = true; reasons.push(`ministry:${hit}`); }
    else missing.push('ministry');
  }

  // 3. Value
  let valueOk = false;
  const v = tender.estimatedValue;
  if (v === null || v === undefined || Number.isNaN(v)) {
    missing.push('value');
  } else {
    const minOk = profile.value_min === null || v >= profile.value_min;
    const maxOk = profile.value_max === null || v <= profile.value_max;
    if (minOk && maxOk) { valueOk = true; reasons.push('value:in-range'); }
    else missing.push('value');
  }

  return { inScope: keywordOk && ministryOk && valueOk, reasons, missing };
}

/** Partition a scraped batch: `capture` (in-scope, up to `cap`), `overflow`
 * (in-scope beyond the cap), `skipped` (out of scope). */
export function splitByScope(
  tenders: TenderData[],
  profile: ScopeProfile,
  cap: number,
): { capture: TenderData[]; overflow: TenderData[]; skipped: TenderData[] } {
  const inScope: TenderData[] = [];
  const skipped: TenderData[] = [];
  for (const t of tenders) {
    if (matchesScope(t, profile).inScope) inScope.push(t);
    else skipped.push(t);
  }
  return { capture: inScope.slice(0, cap), overflow: inScope.slice(cap), skipped };
}
