import { useState, useEffect } from 'react';
import { getPortalHealth, getPortalAlerts } from '../lib/api';
import type { PortalHealthInfo, PortalAlert } from '../types/monitoring';

export function usePortalHealth() {
  const [health, setHealth] = useState<PortalHealthInfo[]>([]);
  const [alerts, setAlerts] = useState<PortalAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      getPortalHealth().catch(() => []),
      getPortalAlerts().catch(() => []),
    ]).then(([h, a]) => {
      setHealth(h);
      setAlerts(a);
      setLoading(false);
    }).catch((err) => {
      setError(err.message);
      setLoading(false);
    });
  }, []);

  return { health, alerts, loading, error };
}
