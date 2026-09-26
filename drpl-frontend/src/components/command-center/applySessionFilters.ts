import type { CommandCenterSession } from '../../types/command-center';

export type SessionStatusFilter =
  | 'all'
  | 'draft'
  | 'submitted'
  | 'under_review'
  | 'approved'
  | 'rejected'
  | 'revision_requested';
export type SessionModeFilter = 'all' | 'tender_linked' | 'standalone';
export type SessionDateRange = 'all' | 'last_7d' | 'last_30d' | 'custom';
export type SessionSortKind = 'recency' | 'created' | 'name' | 'status';

export interface SessionFilter {
  status: SessionStatusFilter;
  mode: SessionModeFilter;
  dateRange: SessionDateRange;
  /** ISO date string (YYYY-MM-DD). Used only when dateRange === 'custom'. */
  customStart?: string;
  /** ISO date string (YYYY-MM-DD). Used only when dateRange === 'custom'. */
  customEnd?: string;
}

export const DEFAULT_FILTER: SessionFilter = {
  status: 'all',
  mode: 'all',
  dateRange: 'all',
};

/**
 * Number of filter chips currently set to a non-'all' value. Used by the
 * "N filters" badge.
 */
export function countActiveFilters(filter: SessionFilter): number {
  let n = 0;
  if (filter.status !== 'all') n++;
  if (filter.mode !== 'all') n++;
  if (filter.dateRange !== 'all') n++;
  return n;
}

/**
 * Apply filter then sort to a session list. Both operations are pure — same
 * inputs always produce the same output. The input array is not mutated.
 */
export function applySessionFilters(
  sessions: CommandCenterSession[],
  filter: SessionFilter,
  sort: SessionSortKind,
): CommandCenterSession[] {
  const filtered = sessions.filter((s) => matchesFilter(s, filter));
  return [...filtered].sort(comparatorFor(sort));
}

// ── Internals ───────────────────────────────────────────────────────────────

function matchesFilter(s: CommandCenterSession, f: SessionFilter): boolean {
  if (f.status !== 'all' && s.status !== f.status) return false;
  if (f.mode !== 'all' && s.mode !== f.mode) return false;

  if (f.dateRange !== 'all') {
    const created = Date.parse(s.created_at);
    if (Number.isNaN(created)) return false;
    const cutoff = computeCutoff(f);
    if (cutoff.from !== null && created < cutoff.from) return false;
    if (cutoff.to !== null && created > cutoff.to) return false;
  }

  return true;
}

function computeCutoff(f: SessionFilter): { from: number | null; to: number | null } {
  if (f.dateRange === 'last_7d') {
    return { from: Date.now() - 7 * 24 * 60 * 60 * 1000, to: null };
  }
  if (f.dateRange === 'last_30d') {
    return { from: Date.now() - 30 * 24 * 60 * 60 * 1000, to: null };
  }
  if (f.dateRange === 'custom') {
    // Inclusive on both ends: customStart 00:00:00, customEnd 23:59:59.999
    const from = f.customStart ? Date.parse(`${f.customStart}T00:00:00.000Z`) : null;
    const to = f.customEnd ? Date.parse(`${f.customEnd}T23:59:59.999Z`) : null;
    return {
      from: from !== null && !Number.isNaN(from) ? from : null,
      to: to !== null && !Number.isNaN(to) ? to : null,
    };
  }
  return { from: null, to: null };
}

function sessionTitle(s: CommandCenterSession): string {
  return (s.tender_id ? s.tender_title : null) || s.title || 'Untitled Session';
}

function recencyTimestamp(s: CommandCenterSession): string {
  return s.updated_at || s.created_at;
}

function comparatorFor(sort: SessionSortKind) {
  if (sort === 'recency') {
    return (a: CommandCenterSession, b: CommandCenterSession) =>
      recencyTimestamp(b).localeCompare(recencyTimestamp(a));
  }
  if (sort === 'created') {
    return (a: CommandCenterSession, b: CommandCenterSession) =>
      b.created_at.localeCompare(a.created_at);
  }
  if (sort === 'name') {
    return (a: CommandCenterSession, b: CommandCenterSession) =>
      sessionTitle(a).toLowerCase().localeCompare(sessionTitle(b).toLowerCase());
  }
  // status — primary asc by status string, fallback to recency
  return (a: CommandCenterSession, b: CommandCenterSession) => {
    const cmp = (a.status || '').localeCompare(b.status || '');
    if (cmp !== 0) return cmp;
    return recencyTimestamp(b).localeCompare(recencyTimestamp(a));
  };
}
