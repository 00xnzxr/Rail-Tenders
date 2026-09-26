import React, { useEffect, useState } from 'react';
import { ScopeProfile, KeywordRunStat } from '../../utils/types';

/**
 * Phase 7 — surfaces the synced Tender Scope Profile and per-keyword
 * GeM auto-search stats. Two action buttons trigger the service-worker
 * orchestration handlers (START_GEM_AUTO_SEARCH and START_IREPS_ELIGIBLE_SCAN).
 */
export default function KeywordPanel() {
  const [profile, setProfile] = useState<ScopeProfile | null>(null);
  const [stats, setStats] = useState<Record<string, KeywordRunStat>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  useEffect(() => {
    refreshFromStorage();
    // Always fetch fresh on open so a recent admin edit is reflected.
    syncFromBackend();

    const listener = (changes: { [k: string]: chrome.storage.StorageChange }) => {
      if ('scopeProfile' in changes) setProfile(changes.scopeProfile.newValue || null);
      if ('gemAutoSearchStats' in changes) setStats(changes.gemAutoSearchStats.newValue || {});
    };
    chrome.storage.onChanged.addListener(listener);
    return () => chrome.storage.onChanged.removeListener(listener);
  }, []);

  const refreshFromStorage = () => {
    chrome.storage.local.get(['scopeProfile', 'gemAutoSearchStats'], (r) => {
      setProfile(r.scopeProfile || null);
      setStats(r.gemAutoSearchStats || {});
    });
  };

  const syncFromBackend = () => {
    chrome.runtime.sendMessage({ type: 'SYNC_SCOPE_PROFILE' }, (resp) => {
      if (resp?.profile) setProfile(resp.profile);
    });
  };

  const startGem = () => {
    setBusy('gem');
    setError(null);
    setInfo(null);
    chrome.runtime.sendMessage({ type: 'START_GEM_AUTO_SEARCH' }, (resp) => {
      setBusy(null);
      if (resp?.error) setError(resp.error);
      else setInfo('Auto-search started — keyword stats will populate below.');
    });
  };

  const startIreps = () => {
    setBusy('ireps');
    setError(null);
    setInfo(null);
    chrome.runtime.sendMessage({ type: 'START_IREPS_ELIGIBLE_SCAN' }, (resp) => {
      setBusy(null);
      if (resp?.error) setError(resp.error);
      else setInfo(`Eligible-items scan started${resp?.extracted ? ` — ${resp.extracted} flagged rows` : ''}.`);
    });
  };

  const startIrepsSearch = () => {
    setBusy('ireps-search');
    setError(null);
    setInfo(null);
    chrome.runtime.sendMessage({ type: 'START_IREPS_AUTO_SEARCH' }, (resp) => {
      setBusy(null);
      if (resp?.error) setError(resp.error);
      else setInfo('IREPS search submitted. Results will be extracted when the page loads.');
    });
  };

  const allKeywords = (profile?.keyword_groups || []).flatMap((g) =>
    (g.keywords || []).map((k) => ({ keyword: k, group: g.label })),
  );

  return (
    <div className="mb-5">
      <h3 className="text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-2.5">
        Scope-Driven Sync
      </h3>

      <div className="grid grid-cols-3 gap-2 mb-3">
        <button
          onClick={startGem}
          disabled={busy !== null || !profile?.is_active || allKeywords.length === 0}
          className="py-2 bg-blue-600 text-white text-xs font-semibold rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors shadow-sm"
        >
          {busy === 'gem' ? 'Starting…' : 'Run Auto-Search'}
        </button>
        <button
          onClick={startIreps}
          disabled={busy !== null}
          className="py-2 bg-indigo-600 text-white text-xs font-semibold rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors shadow-sm"
        >
          {busy === 'ireps' ? 'Starting…' : 'Scan Eligible'}
        </button>
        <button
          onClick={startIrepsSearch}
          disabled={busy !== null}
          className="py-2 bg-teal-600 text-white text-xs font-semibold rounded-lg hover:bg-teal-700 disabled:opacity-50 transition-colors shadow-sm"
        >
          {busy === 'ireps-search' ? 'Starting…' : 'IREPS Search'}
        </button>
      </div>

      {error && (
        <div className="px-3 py-2 mb-2 bg-red-50 border border-red-100 rounded-lg">
          <p className="text-[11px] text-red-600">{error}</p>
        </div>
      )}
      {info && !error && (
        <div className="px-3 py-2 mb-2 bg-emerald-50 border border-emerald-100 rounded-lg">
          <p className="text-[11px] text-emerald-700">{info}</p>
        </div>
      )}

      {!profile && (
        <p className="text-[11px] text-slate-400">
          Loading scope profile…
        </p>
      )}

      {profile && allKeywords.length === 0 && (
        <div className="px-3 py-2 bg-amber-50 border border-amber-100 rounded-lg">
          <p className="text-[11px] text-amber-700">
            No keywords configured. Add some on the Scope admin page.
          </p>
        </div>
      )}

      {profile && allKeywords.length > 0 && (
        <div className="bg-white border border-slate-100 rounded-lg overflow-hidden">
          <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-100 flex justify-between">
            <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide">
              Keywords ({allKeywords.length})
            </span>
            <button
              onClick={syncFromBackend}
              className="text-[10px] text-slate-500 hover:text-blue-600"
            >
              Refresh
            </button>
          </div>
          <ul className="max-h-40 overflow-y-auto divide-y divide-slate-50">
            {allKeywords.map(({ keyword, group }) => {
              const s = stats[keyword];
              return (
                <li key={keyword} className="px-3 py-1.5 flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-[11px] font-semibold text-slate-700 truncate" title={keyword}>
                      {keyword}
                    </p>
                    <p className="text-[10px] text-slate-400 truncate">{group}</p>
                  </div>
                  <div className="text-[10px] text-slate-500 text-right shrink-0">
                    {s ? (
                      <>
                        <span className="font-semibold text-blue-600">{s.hits}</span> hits
                        <span className="text-slate-300"> · </span>
                        {s.pages}p
                        {s.errors > 0 && <span className="text-red-500"> · {s.errors} err</span>}
                      </>
                    ) : (
                      <span className="text-slate-300">never run</span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {profile?.relevance_threshold !== undefined && (
        <p className="text-[10px] text-slate-400 mt-2">
          Hits below {Math.round((profile.relevance_threshold || 0.6) * 100)}% relevance are hidden in the dashboard.
        </p>
      )}
    </div>
  );
}
