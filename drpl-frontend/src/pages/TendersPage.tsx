import { useState, useCallback, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Sparkles, Loader2, X, CheckCircle2, XCircle, AlertCircle, Archive, Trash2, CheckSquare, CalendarX } from 'lucide-react';
import Header from '../components/layout/Header';
import SegmentFunnel from '../components/tenders/SegmentFunnel';
import FilterBar from '../components/tenders/FilterBar';
import TenderCardList from '../components/tenders/TenderCardList';
import Pagination from '../components/ui/Pagination';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import AdvancedToggle from '../components/ui/AdvancedToggle';
import { useTenders } from '../hooks/useTenders';
import { useAdvancedMode } from '../hooks/useAdvancedMode';
import { triggerBatchTenderAnalysis, bulkArchiveTenders, bulkDeleteTenders, getTenderViewCounts, type BatchAnalysisResponse, type TenderViewCounts } from '../lib/api';
import { resolveView } from '../lib/tenderViews';
import type { TenderFilters as Filters } from '../types/tender';

const PAGE_SIZE = 100;
const MAX_BATCH = 50;

export default function TendersPage() {
  // The active decision-stage view comes from the route (/tenders/view/:view);
  // bare /tenders is the "All" view. The view supplies the base filters;
  // Refine narrows on top of them.
  const { view: viewSlug } = useParams();
  const view = resolveView(viewSlug);

  const [refine, setRefine] = useState<Partial<Filters>>({});
  const [refineCount, setRefineCount] = useState(0);
  const [offset, setOffset] = useState(0);

  // Tenders past their closing date are hidden by default — they can no longer
  // be bid on, so they are noise in every pile. Purely a view filter: the rows
  // are untouched in the DB and one toggle away. Persisted so the choice sticks
  // across navigation.
  const [showExpired, setShowExpired] = useState(
    () => localStorage.getItem('drpl_show_expired') === '1',
  );
  const toggleShowExpired = (v: boolean) => {
    setShowExpired(v);
    localStorage.setItem('drpl_show_expired', v ? '1' : '0');
    setOffset(0);   // page 1 — the result set just changed size
  };

  // Base filters from the view + Refine deltas + pagination/sort.
  const filters: Filters = {
    ...view.baseFilters(),
    ...refine,
    exclude_expired: !showExpired,   // sent either way — the route defaults to true
    limit: PAGE_SIZE,
    offset,
    sort_by: refine.sort_by || 'relevance',
  } as Filters;

  const { tenders, total, loading, refetch } = useTenders(filters);

  // Funnel segment counts — one request, refreshed when bulk actions move
  // tenders between piles.
  // Counts use the SAME expiry filter as the list, so the piles never
  // contradict the rows on screen.
  const [counts, setCounts] = useState<TenderViewCounts | null>(null);
  const loadCounts = useCallback(() => {
    getTenderViewCounts(!showExpired).then(setCounts).catch(() => setCounts(null));
  }, [showExpired]);
  useEffect(() => { loadCounts(); }, [loadCounts]);

  // Progressive disclosure — batch/select tools are power-user features,
  // hidden by default so the page reads as a clean "browse tenders" list.
  const { advanced, toggle: toggleAdvanced } = useAdvancedMode('tenders');

  // Multi-select state
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  // Reset Refine + pagination + selection whenever the view changes.
  useEffect(() => {
    setRefine({});
    setRefineCount(0);
    setOffset(0);
    setSelectedIds(new Set());
  }, [viewSlug]);

  // Leaving Advanced mode clears any in-progress selection + banners so the
  // simple view is never left in a half-selected state.
  useEffect(() => {
    if (!advanced) {
      setSelectedIds(new Set());
      setConfirmDelete(false);
    }
  }, [advanced]);

  // Batch analysis state
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchResult, setBatchResult] = useState<BatchAnalysisResponse | null>(null);
  const [batchError, setBatchError] = useState<string | null>(null);

  // Bulk archive / delete state
  const [bulkLoading, setBulkLoading] = useState<'archive' | 'delete' | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [bulkSuccess, setBulkSuccess] = useState<string | null>(null);

  const applyRefine = (delta: Record<string, any>) => {
    const cleaned = Object.fromEntries(
      Object.entries(delta).filter(([, v]) => v !== undefined && v !== '' && v !== null),
    ) as Partial<Filters>;
    setRefine(cleaned);
    // Count active refinements (sort is a preference, not a filter).
    setRefineCount(Object.keys(cleaned).filter((k) => k !== 'sort_by').length);
    setOffset(0);
    setSelectedIds(new Set());
  };

  const toggleSelect = useCallback((id: number) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setSelectedIds(prev => {
      const visibleIds = tenders.map(t => t.id);
      const allVisibleSelected = visibleIds.every(id => prev.has(id));
      if (allVisibleSelected) {
        const next = new Set(prev);
        for (const id of visibleIds) next.delete(id);
        return next;
      }
      const next = new Set(prev);
      for (const id of visibleIds) next.add(id);
      return next;
    });
  }, [tenders]);

  const clearSelection = () => setSelectedIds(new Set());

  const runBatchAnalysis = async () => {
    if (selectedIds.size === 0) return;
    if (selectedIds.size > MAX_BATCH) {
      setBatchError(`Maximum ${MAX_BATCH} tenders per batch.`);
      return;
    }
    setBatchRunning(true);
    setBatchError(null);
    setBatchResult(null);
    try {
      const result = await triggerBatchTenderAnalysis(Array.from(selectedIds));
      setBatchResult(result);
      clearSelection();
    } catch (err: any) {
      setBatchError(err?.response?.data?.detail || err?.message || 'Batch analysis failed.');
    } finally {
      setBatchRunning(false);
    }
  };

  const runBulkArchive = async () => {
    if (selectedIds.size === 0) return;
    setBulkLoading('archive');
    setBulkSuccess(null);
    setBatchError(null);
    try {
      const result = await bulkArchiveTenders(Array.from(selectedIds));
      setBulkSuccess(`${result.archived} tender${result.archived === 1 ? '' : 's'} archived.`);
      clearSelection();
      refetch();
      loadCounts();
    } catch (err: any) {
      setBatchError(err?.response?.data?.detail || err?.message || 'Archive failed.');
    } finally {
      setBulkLoading(null);
    }
  };

  const runBulkDelete = async () => {
    if (selectedIds.size === 0) return;
    setBulkLoading('delete');
    setBulkSuccess(null);
    setBatchError(null);
    try {
      const result = await bulkDeleteTenders(Array.from(selectedIds));
      setBulkSuccess(`${result.deleted} tender${result.deleted === 1 ? '' : 's'} permanently deleted.`);
      clearSelection();
      setConfirmDelete(false);
      refetch();
    } catch (err: any) {
      setBatchError(err?.response?.data?.detail || err?.message || 'Delete failed.');
    } finally {
      setBulkLoading(null);
    }
  };

  return (
    <>
      <Header title="Tenders" subtitle="Find, compare, and decide what to pursue" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {/* Page head */}
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div>
            <p className="text-xs font-extrabold uppercase tracking-[0.14em] text-emerald-700 dark:text-emerald-400">Opportunity desk</p>
            <h1 className="mt-1 text-2xl font-extrabold tracking-tight">Choose the right tender</h1>
          </div>
          <p className="text-sm text-muted-foreground tabular-nums">
            {counts ? <><span className="font-semibold text-foreground">{counts.all.toLocaleString()}</span> tenders across every portal</> : ' '}
          </p>
        </div>

        {/* Signature: the funnel */}
        <SegmentFunnel counts={counts} activeKey={view.key} />

        {/* Active view blurb */}
        <p className="text-sm text-muted-foreground max-w-[68ch]">
          {view.blurb}
          {!showExpired && (
            <span className="text-muted-foreground/80">
              {' '}Tenders past their closing date are hidden — they are still
              stored, and “Show expired” brings them back.
            </span>
          )}
        </p>

        {/* Filter bar + Select mode */}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex-1 min-w-[280px]">
            {/* Remount per view so the filter chips reset when the pile changes
                (the parent clears `refine` on view change — keep them in sync). */}
            <FilterBar key={viewSlug ?? 'all'} onApply={applyRefine} />
          </div>
          <div className="flex items-center gap-4">
            <AdvancedToggle
              checked={showExpired}
              onCheckedChange={toggleShowExpired}
              label="Show expired"
              icon={CalendarX}
            />
            <AdvancedToggle checked={advanced} onCheckedChange={toggleAdvanced} label="Select" icon={CheckSquare} />
          </div>
        </div>

        {advanced && selectedIds.size === 0 && (
          <p className="text-xs text-muted-foreground -mt-1">
            Select tenders to archive, delete, or run deep analysis in bulk.
          </p>
        )}

        {/* Batch action bar — advanced mode only, when selections exist */}
        {advanced && selectedIds.size > 0 && (
          <div className="flex items-center justify-between bg-accent/10 border border-accent/20 rounded-lg px-4 py-3">
            <div className="flex items-center gap-3 text-sm text-accent">
              <span className="font-medium">{selectedIds.size} tender{selectedIds.size === 1 ? '' : 's'} selected</span>
              {selectedIds.size > MAX_BATCH && (
                <span className="text-red-600 dark:text-red-400 flex items-center gap-1">
                  <AlertCircle size={14} /> Max {MAX_BATCH} per batch for analysis — deselect some.
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={clearSelection}
                disabled={batchRunning || bulkLoading !== null}
                className="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                Clear
              </button>

              {/* Archive */}
              <button
                onClick={runBulkArchive}
                disabled={batchRunning || bulkLoading !== null}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-amber-100 dark:bg-amber-500/20 text-amber-800 dark:text-amber-400 border border-amber-300 dark:border-amber-500/30 rounded-lg hover:bg-amber-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {bulkLoading === 'archive' ? <Loader2 size={14} className="animate-spin" /> : <Archive size={14} />}
                Archive
              </button>

              {/* Delete — with inline confirmation */}
              {confirmDelete ? (
                <div className="flex items-center gap-1.5">
                  <span className="text-sm text-red-700 dark:text-red-400 font-medium">Delete {selectedIds.size}?</span>
                  <button
                    onClick={runBulkDelete}
                    disabled={bulkLoading !== null}
                    className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {bulkLoading === 'delete' ? <Loader2 size={14} className="animate-spin" /> : null}
                    Confirm
                  </button>
                  <button
                    onClick={() => setConfirmDelete(false)}
                    disabled={bulkLoading !== null}
                    className="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmDelete(true)}
                  disabled={batchRunning || bulkLoading !== null}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-500/20 rounded-lg hover:bg-red-100 dark:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Trash2 size={14} />
                  Delete
                </button>
              )}

              <button
                onClick={runBatchAnalysis}
                disabled={batchRunning || bulkLoading !== null || selectedIds.size > MAX_BATCH}
                className="flex items-center gap-2 px-4 py-1.5 text-sm font-medium bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {batchRunning ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                {batchRunning ? 'Analyzing…' : `Run deep analysis on ${selectedIds.size}`}
              </button>
            </div>
          </div>
        )}

        {/* Batch error banner */}
        {batchError && (
          <div className="flex items-start justify-between bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg px-4 py-3">
            <div className="flex items-start gap-2 text-sm text-red-800 dark:text-red-400">
              <XCircle size={16} className="mt-0.5 shrink-0" />
              <span>{batchError}</span>
            </div>
            <button onClick={() => setBatchError(null)} className="text-red-600 dark:text-red-400 hover:text-red-800 dark:text-red-400" aria-label="Dismiss">
              <X size={16} />
            </button>
          </div>
        )}

        {/* Bulk action success banner */}
        {bulkSuccess && (
          <div className="flex items-start justify-between bg-emerald-50 dark:bg-emerald-500/15 border border-emerald-200 dark:border-emerald-500/20 rounded-lg px-4 py-3">
            <div className="flex items-start gap-2 text-sm text-emerald-800 dark:text-emerald-400">
              <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
              <span>{bulkSuccess}</span>
            </div>
            <button onClick={() => setBulkSuccess(null)} className="text-emerald-600 dark:text-emerald-400 hover:text-emerald-800 dark:text-emerald-400" aria-label="Dismiss">
              <X size={16} />
            </button>
          </div>
        )}

        {/* Batch result panel */}
        {batchResult && (
          <BatchResultPanel result={batchResult} onDismiss={() => setBatchResult(null)} />
        )}

        {/* Long-running indicator */}
        {batchRunning && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground bg-muted/40 border border-border rounded-lg px-4 py-3">
            <Loader2 size={14} className="animate-spin" />
            <span>
              Running deep analysis on {selectedIds.size || batchResult?.batch_size} tenders — this can take several minutes.
              Concurrency is capped server-side to avoid rate limits. Keep this tab open.
            </span>
          </div>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : tenders.length === 0 ? (
          <div className="bg-card border border-border rounded-xl p-8 text-center">
            <p className="text-sm text-muted-foreground">
              {refineCount > 0
                ? 'No matches. Clear the filters or switch piles.'
                : view.key === 'to_bid'
                  ? 'Nothing to pursue yet. Try “Review” or “New”.'
                  : view.key === 'discarded'
                    ? 'Nothing set aside — every scored tender made the cut.'
                    : 'Nothing here yet.'}
            </p>
          </div>
        ) : (
          <>
            <TenderCardList
              tenders={tenders}
              searchQuery={refine.search}
              // Selection UI only renders in Advanced mode — passing these
              // props is what turns the checkboxes on (TenderCardList
              // derives `selectable` from onToggleSelect being defined).
              selectedIds={advanced ? selectedIds : undefined}
              onToggleSelect={advanced ? toggleSelect : undefined}
              onToggleAll={advanced ? toggleAll : undefined}
              variant={view.key === 'discarded' ? 'discarded' : 'default'}
            />
            <Pagination
              offset={offset}
              limit={PAGE_SIZE}
              total={total}
              onChange={(newOffset) => setOffset(newOffset)}
            />
          </>
        )}
      </div>
    </>
  );
}

function BatchResultPanel({
  result,
  onDismiss,
}: {
  result: BatchAnalysisResponse;
  onDismiss: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 bg-muted/40 border-b border-border">
        <div className="flex items-center gap-4 text-sm">
          <span className="font-medium text-foreground">
            Batch done — {result.batch_size} tender{result.batch_size === 1 ? '' : 's'}
          </span>
          <span className="flex items-center gap-1 text-emerald-700 dark:text-emerald-400">
            <CheckCircle2 size={14} /> {result.succeeded} succeeded
          </span>
          {result.failed > 0 && (
            <span className="flex items-center gap-1 text-red-700 dark:text-red-400">
              <XCircle size={14} /> {result.failed} failed
            </span>
          )}
          {result.skipped > 0 && (
            <span className="flex items-center gap-1 text-amber-700 dark:text-amber-400">
              <AlertCircle size={14} /> {result.skipped} skipped
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setExpanded(x => !x)}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            {expanded ? 'Hide details' : 'Show details'}
          </button>
          <button onClick={onDismiss} className="text-muted-foreground hover:text-muted-foreground" aria-label="Dismiss">
            <X size={16} />
          </button>
        </div>
      </div>
      {expanded && (
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-card border-b border-border text-xs text-muted-foreground uppercase">
                <th className="px-4 py-2 text-left">Tender ID</th>
                <th className="px-4 py-2 text-left">Status</th>
                <th className="px-4 py-2 text-right">Requirements</th>
                <th className="px-4 py-2 text-right">Critical Flags</th>
                <th className="px-4 py-2 text-right">Docs</th>
                <th className="px-4 py-2 text-left">Error</th>
              </tr>
            </thead>
            <tbody>
              {result.results.map(r => (
                <tr key={r.tender_id} className="border-b border-border">
                  <td className="px-4 py-2 font-mono text-foreground">{r.tender_id}</td>
                  <td className="px-4 py-2">
                    <StatusPill status={r.status} />
                  </td>
                  <td className="px-4 py-2 text-right text-foreground">{r.total_requirements ?? '—'}</td>
                  <td className="px-4 py-2 text-right text-foreground">{r.total_critical_flags ?? '—'}</td>
                  <td className="px-4 py-2 text-right text-foreground">{r.documents_analyzed ?? '—'}</td>
                  <td className="px-4 py-2 text-red-600 dark:text-red-400 max-w-xs truncate" title={r.error}>{r.error || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    completed: 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20 dark:bg-emerald-500/15 dark:text-emerald-400 dark:border-emerald-500/20',
    failed: 'bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 border-red-200 dark:border-red-500/20 dark:bg-red-500/15 dark:text-red-400 dark:border-red-500/20',
    skipped: 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-200 dark:border-amber-500/20 dark:bg-amber-500/15 dark:text-amber-400 dark:border-amber-500/20',
    in_progress: 'bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-500/15 dark:text-blue-400 dark:border-blue-500/20',
  };
  const cls = map[status] ?? 'bg-muted text-foreground border-border';
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium border ${cls}`}>
      {status}
    </span>
  );
}
