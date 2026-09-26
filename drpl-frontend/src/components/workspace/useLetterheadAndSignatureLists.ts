import { useEffect, useState } from 'react';
import { getLetterheadTemplates, getSignatures } from '../../lib/api';

type Lists = { templates: any[]; signatures: any[] };

const CACHE_TTL_MS = 5 * 60 * 1000;

let cache: { value: Lists; fetchedAt: number } | null = null;
let inflight: Promise<Lists> | null = null;

async function load(): Promise<Lists> {
  if (cache && Date.now() - cache.fetchedAt < CACHE_TTL_MS) {
    return cache.value;
  }
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const [templates, signatures] = await Promise.all([
        getLetterheadTemplates(),
        getSignatures(),
      ]);
      const value: Lists = { templates, signatures };
      cache = { value, fetchedAt: Date.now() };
      return value;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

export function useLetterheadAndSignatureLists(): {
  templates: any[];
  signatures: any[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
} {
  const [templates, setTemplates] = useState<any[]>(cache?.value.templates ?? []);
  const [signatures, setSignatures] = useState<any[]>(cache?.value.signatures ?? []);
  const [loading, setLoading] = useState(!cache);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(!cache);
    load()
      .then((v) => {
        if (cancelled) return;
        setTemplates(v.templates);
        setSignatures(v.signatures);
        setError(null);
      })
      .catch((e: any) => {
        if (cancelled) return;
        setError(e?.response?.data?.detail || 'Failed to load letterheads and signatures');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  return {
    templates,
    signatures,
    loading,
    error,
    refresh: () => {
      cache = null;
      setTick((t) => t + 1);
    },
  };
}
