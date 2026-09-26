import { useState, useEffect } from 'react';
import { getScrapeLogs } from '../lib/api';
import type { ScrapeSession } from '../types/scrape';

export function useScrapeLog(portal?: string) {
  const [logs, setLogs] = useState<ScrapeSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchLogs = () => {
      getScrapeLogs(portal)
        .then(setLogs)
        .catch((err) => setError(err.message))
        .finally(() => setLoading(false));
    };

    fetchLogs();
    const interval = setInterval(fetchLogs, 10000); // refresh every 10s
    return () => clearInterval(interval);
  }, [portal]);

  return { logs, loading, error };
}
