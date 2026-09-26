import React, { useEffect, useState } from 'react';

const SUPPORTED_HOSTS: Record<string, string> = {
  'ireps.gov.in': 'Portal A',
  'gem.gov.in': 'Portal B',
  'mkp.gem.gov.in': 'Portal B',
  'tendertiger.com': 'Aggregator 1',
  'www.tendertiger.com': 'Aggregator 1',
  'bidassist.com': 'Aggregator 2',
  'www.bidassist.com': 'Aggregator 2',
  'tenderdetail.com': 'Aggregator 3',
  'www.tenderdetail.com': 'Aggregator 3',
  'tendersinfo.com': 'Aggregator 4',
  'www.tendersinfo.com': 'Aggregator 4',
  'projectstoday.com': 'Aggregator 5',
  'www.projectstoday.com': 'Aggregator 5',
};

const TENDER_LIMIT_OPTIONS = [5, 10, 25, 50, 100];

export default function ScrapeControls() {
  const [scraping, setScraping] = useState(false);
  const [currentSite, setCurrentSite] = useState<string | null>(null);
  const [currentTabId, setCurrentTabId] = useState<number | null>(null);
  const [tenderLimit, setTenderLimit] = useState(10);
  const [scrapeError, setScrapeError] = useState<string | null>(null);

  useEffect(() => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]?.url) {
        const url = new URL(tabs[0].url);
        setCurrentTabId(tabs[0].id || null);
        for (const [domain, name] of Object.entries(SUPPORTED_HOSTS)) {
          if (url.hostname === domain || url.hostname.endsWith('.' + domain)) {
            setCurrentSite(name);
            return;
          }
        }
        setCurrentSite(null);
      }
    });
    // Load saved tender limit
    chrome.storage.local.get('tenderLimit', (r) => {
      if (r.tenderLimit) setTenderLimit(r.tenderLimit);
    });
  }, []);

  const startScrape = () => {
    if (!currentTabId) return;
    setScraping(true);
    setScrapeError(null);
    // Persist the limit so the content script can read it
    chrome.storage.local.set({ tenderLimit });
    chrome.runtime.sendMessage(
      { type: 'SCRAPE_CURRENT_TAB', payload: { tabId: currentTabId, tenderLimit } },
      (response) => {
        if (chrome.runtime.lastError || response?.error) {
          const msg = response?.error || chrome.runtime.lastError?.message || 'Unknown error';
          setScrapeError(msg);
          setScraping(false);
        } else {
          const count = response?.extracted ?? 0;
          if (count === 0) {
            setScrapeError('No items found on this page. Make sure the listing is visible.');
          }
          setTimeout(() => setScraping(false), 1500);
        }
      }
    );
  };

  return (
    <div className="mb-5">
      <h3 className="text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-2.5">
        Scrape Current Page
      </h3>

      {currentSite ? (
        <div className="space-y-2.5">
          {/* Portal detected badge */}
          <div className="flex items-center gap-2 px-3 py-2 bg-emerald-50 border border-emerald-100 rounded-lg">
            <span className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-pulse shrink-0" />
            <span className="text-xs font-semibold text-emerald-700">{currentSite}</span>
            <span className="text-[11px] text-emerald-500 truncate">— supported portal detected</span>
          </div>

          {/* Tender limit selector + button */}
          <div className="flex gap-2">
            <div className="flex items-center gap-2 bg-white border border-slate-200 rounded-lg px-2.5 py-1.5 shrink-0">
              <label className="text-[11px] font-medium text-slate-400 whitespace-nowrap">Tenders</label>
              <select
                value={tenderLimit}
                onChange={(e) => setTenderLimit(Number(e.target.value))}
                className="text-xs font-semibold text-slate-700 bg-transparent focus:outline-none cursor-pointer"
              >
                {TENDER_LIMIT_OPTIONS.map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </div>

            <button
              onClick={startScrape}
              disabled={scraping}
              className="flex-1 py-2 bg-blue-600 text-white text-xs font-semibold rounded-lg hover:bg-blue-700 active:bg-blue-800 disabled:opacity-50 transition-colors shadow-sm"
            >
              {scraping ? 'Starting…' : `Scrape ${tenderLimit} Tenders`}
            </button>
          </div>

          {scrapeError && (
            <div className="px-3 py-2 bg-red-50 border border-red-100 rounded-lg">
              <p className="text-[11px] text-red-600">{scrapeError}</p>
            </div>
          )}

          <p className="text-[11px] text-slate-400 leading-relaxed">
            Quality mode: each tender is fully processed before the next. Runs in background if you switch tabs.
          </p>
        </div>
      ) : (
        <div className="px-4 py-4 bg-white border border-slate-100 rounded-lg text-center">
          <p className="text-xs font-medium text-slate-500 mb-1">
            Navigate to a supported portal to start syncing.
          </p>
        </div>
      )}
    </div>
  );
}
