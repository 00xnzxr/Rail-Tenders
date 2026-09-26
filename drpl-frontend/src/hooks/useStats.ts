import { useState, useEffect } from 'react';
import { getTenderStats } from '../lib/api';
import type { TenderStats } from '../types/tender';

export function useStats() {
  const [stats, setStats] = useState<TenderStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTenderStats()
      .then(setStats)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  return { stats, loading, error };
}
