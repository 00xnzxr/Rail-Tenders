import { useEffect, useState } from 'react';
import type { ScrapeJobProgress, TenderProgress } from '../../utils/types';

/** Live per-tender progress panel. Shown during/after a scrape job.
 *  Listens to chrome.storage.onChanged for real-time updates. */
export default function ScrapeProgressPanel() {
  const [job, setJob] = useState<ScrapeJobProgress | null>(null);
  const [pendingHuntCount, setPendingHuntCount] = useState(0);
  const [hunting, setHunting] = useState(false);
  const [huntError, setHuntError] = useState<string | null>(null);

  useEffect(() => {
    chrome.storage.local.get(['scrapeJobProgress', 'pendingDocumentHunt'], (r) => {
      if (r.scrapeJobProgress) setJob(r.scrapeJobProgress);
      setPendingHuntCount((r.pendingDocumentHunt || []).length);
    });
    const listener = (changes: { [k: string]: chrome.storage.StorageChange }) => {
      if ('scrapeJobProgress' in changes) {
        setJob(changes.scrapeJobProgress.newValue || null);
      }
      if ('pendingDocumentHunt' in changes) {
        setPendingHuntCount((changes.pendingDocumentHunt.newValue || []).length);
      }
    };
    chrome.storage.onChanged.addListener(listener);
    return () => chrome.storage.onChanged.removeListener(listener);
  }, []);

  const dismiss = () => {
    chrome.storage.local.remove('scrapeJobProgress');
    setJob(null);
  };

  const startHunt = () => {
    setHunting(true);
    setHuntError(null);
    chrome.runtime.sendMessage({ type: 'START_DOCUMENT_HUNT' }, (resp) => {
      setHunting(false);
      if (resp?.error) setHuntError(resp.error);
    });
  };

  if (!job) return null;

  const completed = job.tenders.filter((t) => t.status === 'completed').length;
  const errors = job.errorsCount;
  const remaining = job.totalTenders - completed - errors;
  const progressPct = job.totalTenders > 0 ? (completed + errors) / job.totalTenders : 0;
  const isDone = job.status === 'completed' || job.status === 'cancelled';

  // ETA calculation (only when running)
  let etaText = '';
  if (job.status === 'running' && job.averageMsPerTender > 0 && remaining > 0) {
    const etaMs = job.averageMsPerTender * remaining;
    const mins = Math.ceil(etaMs / 60000);
    etaText = mins < 1 ? '< 1 min' : `~${mins} min`;
  }

  const current = job.tenders[job.currentTenderIndex];

  return (
    <div className="mb-5">
      <div className="flex items-center justify-between mb-2.5">
        <h3 className="text-[10px] font-semibold text-slate-400 uppercase tracking-widest">
          {isDone ? 'Scrape Complete' : 'Scraping In Progress'}
        </h3>
        {isDone && (
          <button
            onClick={dismiss}
            className="text-[10px] text-slate-400 hover:text-slate-600 font-medium"
          >
            Dismiss ✕
          </button>
        )}
      </div>

      {/* Completion banner */}
      {isDone && (
        <div className={`rounded-lg p-3 mb-2 border ${
          errors > 0 ? 'bg-amber-50 border-amber-200' : 'bg-emerald-50 border-emerald-200'
        }`}>
          <div className="flex items-baseline gap-2">
            <span className="text-base">{errors > 0 ? '⚠' : '✓'}</span>
            <span className={`text-sm font-semibold ${errors > 0 ? 'text-amber-700' : 'text-emerald-700'}`}>
              {completed}/{job.totalTenders} tenders processed
            </span>
          </div>
          <p className={`text-[11px] mt-1 ${errors > 0 ? 'text-amber-600' : 'text-emerald-600'}`}>
            {job.totalDocsUploaded > 0
              ? `${job.totalDocsUploaded} documents uploaded`
              : 'Metadata uploaded — documents pending hunt'}
            {errors > 0 && ` · ${errors} error${errors > 1 ? 's' : ''}`}
          </p>

          {/* Hunt Documents button — only show if there are tenders pending hunt */}
          {pendingHuntCount > 0 && (
            <div className="mt-3 pt-3 border-t border-emerald-200">
              <p className="text-[11px] text-slate-600 mb-2">
                Next step: hunt PDFs from each tender's detail page.
              </p>
              <button
                onClick={startHunt}
                disabled={hunting}
                className="w-full py-2 bg-blue-600 text-white text-xs font-semibold rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors shadow-sm"
              >
                {hunting ? 'Starting…' : `🔎 Hunt Documents (${pendingHuntCount} tenders)`}
              </button>
              {huntError && (
                <p className="text-[11px] text-red-600 mt-1.5">{huntError}</p>
              )}
            </div>
          )}
        </div>
      )}

      {/* Running banner */}
      {!isDone && (
        <div className="bg-white border border-slate-200 rounded-lg p-3 mb-2">
          <div className="flex items-baseline justify-between mb-1.5">
            <span className="text-sm font-semibold text-slate-700">
              Tender {Math.min(job.currentTenderIndex + 1, job.totalTenders)} of {job.totalTenders}
            </span>
            {etaText && <span className="text-[11px] text-slate-500">ETA: {etaText}</span>}
          </div>
          <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-blue-600 h-1.5 rounded-full transition-all"
              style={{ width: `${Math.round(progressPct * 100)}%` }}
            />
          </div>
          <div className="flex gap-3 mt-2 text-[10px]">
            <span className="text-emerald-600 font-medium">{completed} done</span>
            {errors > 0 && <span className="text-red-500 font-medium">{errors} errors</span>}
            <span className="text-slate-400">{remaining} pending</span>
            <span className="text-slate-400 ml-auto">{job.totalDocsUploaded} docs</span>
          </div>
        </div>
      )}

      {/* Current tender step detail */}
      {!isDone && current && (
        <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 mb-2">
          <p className="text-[10px] uppercase tracking-wide text-blue-600 font-semibold mb-1">
            {stepLabel(current.status)}
          </p>
          <p className="text-xs font-medium text-slate-800 truncate" title={current.title}>
            {current.title}
          </p>
          <p className="text-[10px] text-slate-500 font-mono mt-0.5">{current.tenderId}</p>
          {current.status === 'downloading' && current.documentsTotal > 0 && (
            <div className="mt-1.5">
              <p className="text-[11px] text-blue-700 font-medium">
                Documents: {current.documentsUploaded}/{current.documentsTotal}
              </p>
              <div className="w-full bg-blue-100 rounded-full h-1 mt-1 overflow-hidden">
                <div
                  className="bg-blue-500 h-1 rounded-full transition-all"
                  style={{ width: `${Math.round((current.documentsCompleted / Math.max(current.documentsTotal, 1)) * 100)}%` }}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {/* Per-tender status list */}
      <div className="bg-white border border-slate-200 rounded-lg overflow-hidden">
        <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-100">
          <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide">
            Per-tender status
          </span>
        </div>
        <ul className="max-h-48 overflow-y-auto divide-y divide-slate-50">
          {job.tenders.map((t, i) => (
            <TenderRow
              key={t.tenderId + i}
              tender={t}
              isCurrent={i === job.currentTenderIndex && !isDone}
            />
          ))}
        </ul>
      </div>
    </div>
  );
}

function TenderRow({ tender, isCurrent }: { tender: TenderProgress; isCurrent: boolean }) {
  const stepText = stepLabel(tender.status);
  return (
    <li
      className={`px-3 py-1.5 flex items-center gap-2 ${isCurrent ? 'bg-blue-50' : ''}`}
      title={tender.error || ''}
    >
      <StatusIcon status={tender.status} />
      <div className="min-w-0 flex-1">
        <p className="text-[11px] font-medium text-slate-700 truncate" title={tender.title}>
          {tender.title.slice(0, 50)}
        </p>
        <p className="text-[10px] text-slate-400 font-mono truncate">
          {tender.tenderId}
          {tender.status !== 'pending' && tender.status !== 'completed' && (
            <span className="ml-1.5 text-blue-500">— {stepText}</span>
          )}
        </p>
      </div>
      <div className="text-[10px] text-right shrink-0">
        {tender.status === 'completed' && tender.documentsTotal > 0 && (
          <span className="text-emerald-600 font-medium">{tender.documentsUploaded}/{tender.documentsTotal}</span>
        )}
        {tender.status === 'completed' && tender.documentsTotal === 0 && (
          <span className="text-emerald-600 font-medium">done</span>
        )}
        {(tender.status === 'downloading' || tender.status === 'uploading') && (
          <span className="text-blue-600">{tender.documentsUploaded}/{tender.documentsTotal || '?'}</span>
        )}
        {tender.status === 'extracting' && (
          <span className="text-blue-600">...</span>
        )}
        {tender.status === 'error' && (
          <span className="text-red-500 font-medium">error</span>
        )}
        {tender.status === 'pending' && <span className="text-slate-300">—</span>}
      </div>
    </li>
  );
}

function StatusIcon({ status }: { status: TenderProgress['status'] }) {
  if (status === 'completed') return <span className="text-emerald-500 text-sm shrink-0">✓</span>;
  if (status === 'error') return <span className="text-red-500 text-sm shrink-0">✗</span>;
  if (status === 'pending') return <span className="text-slate-300 text-sm shrink-0">⏸</span>;
  return <span className="text-blue-500 text-sm shrink-0 animate-pulse">⟳</span>;
}

function stepLabel(s: TenderProgress['status']): string {
  switch (s) {
    case 'uploading': return 'Uploading metadata';
    case 'extracting': return 'Extracting details';
    case 'downloading': return 'Downloading documents';
    case 'completed': return 'Done';
    case 'error': return 'Error';
    default: return 'Pending';
  }
}
