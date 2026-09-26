import type { CommandCenterSession } from '../../types/command-center';

/**
 * Pure search/rank function for the session palette.
 *
 * Matching: case-insensitive substring across title, tender_title,
 * tender_organisation.
 *
 * Ranking:
 *   1. Exact prefix match on any field (highest)
 *   2. Substring match on any field
 *   3. (non-matching sessions are excluded for non-empty queries)
 *
 * Within each rank bucket, ordered by updated_at desc (falling back to
 * created_at when updated_at is absent), so recently-touched tenders
 * surface first.
 *
 * Empty query returns ALL sessions, recency-sorted.
 */
export function searchSessions(
  sessions: CommandCenterSession[],
  query: string,
): CommandCenterSession[] {
  const q = query.trim().toLowerCase();
  const byRecency = (a: CommandCenterSession, b: CommandCenterSession) => {
    const at = a.updated_at || a.created_at;
    const bt = b.updated_at || b.created_at;
    return bt.localeCompare(at);
  };

  if (q === '') {
    return [...sessions].sort(byRecency);
  }

  const fields = (s: CommandCenterSession): string[] =>
    [s.title, s.tender_title, s.tender_organisation]
      .filter((v): v is string => typeof v === 'string' && v.length > 0)
      .map((v) => v.toLowerCase());

  const rank = (s: CommandCenterSession): 0 | 1 | 2 => {
    const fs = fields(s);
    if (fs.some((f) => f.startsWith(q))) return 0;
    if (fs.some((f) => f.includes(q))) return 1;
    return 2;
  };

  return sessions
    .map((s) => ({ s, r: rank(s) }))
    .filter((x) => x.r < 2)
    .sort((a, b) => (a.r - b.r) || byRecency(a.s, b.s))
    .map((x) => x.s);
}
