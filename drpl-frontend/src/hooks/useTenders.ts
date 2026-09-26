import { useState, useEffect, useCallback } from 'react';
import { getTenders } from '../lib/api';
import type { Tender, TenderFilters } from '../types/tender';

export function useTenders(filters: TenderFilters) {
  const [tenders, setTenders] = useState<Tender[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetch = useCallback(() => {
    setLoading(true);
    setError(null);
    getTenders(filters)
      .then((res) => { setTenders(res.items); setTotal(res.total); })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [filters.portal, filters.search, filters.department, filters.status, filters.priority, filters.workflow_status,
      filters.assigned_to, filters.sort_by, filters.bid_type, filters.location, filters.segment,
      filters.score_min, filters.score_max, filters.value_min, filters.value_max, filters.emd_min,
      filters.emd_max, filters.closing_after, filters.closing_before, filters.eligibility_status,
      filters.include_below_threshold, filters.unscored, filters.exclude_expired,
      filters.limit, filters.offset]);

  useEffect(() => { fetch(); }, [fetch]);

  return { tenders, total, loading, error, refetch: fetch };
}
