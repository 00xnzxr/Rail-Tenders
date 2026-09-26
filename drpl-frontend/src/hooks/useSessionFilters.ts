import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CommandCenterSession } from '../types/command-center';
import {
  applySessionFilters,
  countActiveFilters,
  DEFAULT_FILTER,
  type SessionFilter,
  type SessionSortKind,
} from '../components/command-center/applySessionFilters';

const SORT_STORAGE_KEY = 'drpl_cc_session_sort';
const VALID_SORTS: ReadonlyArray<SessionSortKind> = ['recency', 'created', 'name', 'status'];

function readStoredSort(): SessionSortKind {
  try {
    const raw = localStorage.getItem(SORT_STORAGE_KEY);
    if (raw && (VALID_SORTS as readonly string[]).includes(raw)) {
      return raw as SessionSortKind;
    }
  } catch {
    // ignore
  }
  return 'recency';
}

export interface UseSessionFiltersReturn {
  filter: SessionFilter;
  setFilter: (next: SessionFilter) => void;
  sort: SessionSortKind;
  setSort: (next: SessionSortKind) => void;
  activeFilterCount: number;
  clearFilters: () => void;
  /** Memoized filtered+sorted view of the input list. */
  apply: (sessions: CommandCenterSession[]) => CommandCenterSession[];
}

/**
 * Shared filter+sort state for the Command Center session list.
 * Filter state is in-memory only; sort is persisted to localStorage so a
 * user's preference survives page reloads.
 */
export function useSessionFilters(): UseSessionFiltersReturn {
  const [filter, setFilter] = useState<SessionFilter>(DEFAULT_FILTER);
  const [sort, setSortState] = useState<SessionSortKind>(() => readStoredSort());

  // Persist sort to localStorage on change
  useEffect(() => {
    try {
      localStorage.setItem(SORT_STORAGE_KEY, sort);
    } catch {
      // ignore quota / privacy-mode failures
    }
  }, [sort]);

  const clearFilters = useCallback(() => {
    setFilter(DEFAULT_FILTER);
  }, []);

  const activeFilterCount = useMemo(() => countActiveFilters(filter), [filter]);

  // We DON'T memoize the filtered output here against sessionList — the
  // caller controls when to compute it (typically once per render).
  // Returning a stable function lets callers call it inline.
  const apply = useCallback(
    (sessions: CommandCenterSession[]) => applySessionFilters(sessions, filter, sort),
    [filter, sort],
  );

  return {
    filter,
    setFilter,
    sort,
    setSort: setSortState,
    activeFilterCount,
    clearFilters,
    apply,
  };
}
