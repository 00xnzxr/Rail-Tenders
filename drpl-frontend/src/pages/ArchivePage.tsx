import { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import {
  Archive, RotateCcw, Trash2, Loader2, X, XCircle, CheckCircle2, AlertCircle,
} from 'lucide-react';
import Header from '../components/layout/Header';
import Pagination from '../components/ui/Pagination';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { useAuth } from '@/context/AuthContext';
import { getArchivedTenders, restoreTender, purgeTenders, getTenderViewCounts } from '../lib/api';
import { formatCurrency, formatDate, portalLabel } from '../lib/formatters';
import type { ArchivedTender, ArchiveReason } from '../types/tender';

const PAGE_SIZE = 50;

type ReasonFilter = 'all' | ArchiveReason;

const REASON_TABS: { key: ReasonFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'past_due', label: 'Past due' },
  { key: 'auto_discard', label: 'Auto-discarded' },
  { key: 'manual', label: 'Manual' },
];

/**
 * Whole days until `iso`, or null when there is no date.
 * Only ever called with `purge_at`, which the backend sets exclusively for
 * `past_due` rows — see the safety invariant in ArchiveChip.
 */
export function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return Math.max(0, Math.ceil((ms - Date.now()) / 86_400_000));
}

/**
 * The status chip for one archived row.
 *
 * SAFETY INVARIANT (mirrors the backend): a purge countdown is shown ONLY when
 * the row is `past_due` AND carries a `purge_at`. auto_discard and manual rows
 * are never auto-deleted, so promising a deletion date on them would be a lie.
 */
export function ArchiveChip({ reason, purgeAt }: { reason: ArchiveReason | null; purgeAt: string | null }) {
  const days = reason === 'past_due' ? daysUntil(purgeAt) : null;

  if (days !== null) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md bg-amber-100 dark:bg-amber-500/20 px-2 py-0.5 text-xs font-medium text-amber-800 dark:text-amber-400">
        <AlertCircle size={12} />
        {days === 0 ? 'Deletes today' : `Deletes in ${days} day${days === 1 ? '' : 's'}`}
      </span>
    );
  }

  const label =
    reason === 'auto_discard' ? 'Archived — low score'
    : reason === 'manual' ? 'Archived manually'
    : reason === 'past_due' ? 'Archived — past due'
    : 'Archived';

  return (
    <span className="inline-flex items-center rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
      {label}
    </span>
  );
}

export default function ArchivePage() {
  const { isAdminOrAbove } = useAuth();

  const [reason, setReason] = useState<ReasonFilter>('all');
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ArchivedTender[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const [busyId, setBusyId] = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Every fetch claims a sequence number; only the newest one is allowed to
  // write state. Without this, toggling the reason tabs quickly races two
  // requests and whichever resolves LAST wins — so a slow "Past due" response
  // landing after a fast "Manual" one would paint past-due rows under a
  // Manual tab. The guard covers setLoading too, so a stale response can't
  // clear the spinner for a page it doesn't belong to.
  const reqSeq = useRef(0);

  const load = useCallback((atOffset: number = offset) => {
    const seq = ++reqSeq.current;
    setLoading(true);
    getArchivedTenders({
      limit: PAGE_SIZE,
      offset: atOffset,
      ...(reason === 'all' ? {} : { reason }),
    })
      .then((res) => {
        if (seq !== reqSeq.current) return;
        setItems(res.items);
        setTotal(res.total);
      })
      .catch((err: any) => {
        if (seq !== reqSeq.current) return;
        setItems([]);
        setTotal(0);
        setError(err?.response?.data?.detail || err?.message || 'Could not load the archive.');
      })
      .finally(() => {
        if (seq !== reqSeq.current) return;
        setLoading(false);
      });
  }, [offset, reason]);

  useEffect(() => { load(); }, [load]);

  // Restoring or purging changes the funnel counts on /tenders; refresh them
  // so the sidebar/dashboard numbers don't go stale behind the user's back.
  const refreshCounts = () => { getTenderViewCounts().catch(() => undefined); };

  const changeReason = (next: ReasonFilter) => {
    setReason(next);
    setOffset(0);
    setConfirmId(null);
  };

  /**
   * Reload after a row leaves the list (restored or purged).
   *
   * Reloading at the current offset is wrong when the row we just removed was
   * the ONLY row on a non-first page: the server would return an empty page
   * and the "Nothing archived" empty state would render even though hundreds
   * of archived tenders still exist — on a page about deletion that reads as
   * catastrophic data loss. So step back one page instead.
   *
   * `Math.max(0, ...)` keeps the offset non-negative, and the `offset > 0`
   * guard means page 1 always just reloads in place (there is no page to step
   * back to, and an empty page 1 genuinely means the archive is empty).
   *
   * `load(next)` takes the offset as an argument because `setOffset` is async
   * — the `load` closure would otherwise still hold the stale offset.
   */
  const reloadAfterRemoval = () => {
    if (offset > 0 && items.length === 1) {
      const next = Math.max(0, offset - PAGE_SIZE);
      setOffset(next);
      load(next);
    } else {
      load();
    }
  };

  const runRestore = async (t: ArchivedTender) => {
    setBusyId(t.id);
    setError(null);
    setSuccess(null);
    try {
      await restoreTender(t.id);
      setSuccess(`Restored “${t.title}” — it is back in the active pipeline.`);
      setConfirmId(null);
      reloadAfterRemoval();
      refreshCounts();
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Restore failed.');
    } finally {
      setBusyId(null);
    }
  };

  const runPurge = async (t: ArchivedTender) => {
    setBusyId(t.id);
    setError(null);
    setSuccess(null);
    try {
      const res = await purgeTenders([t.id]);
      // The backend refuses to delete rows that are no longer archived and
      // reports them as `skipped`. Without surfacing that, a row purged by the
      // sweep (or by another admin) a moment ago would just silently vanish
      // from the list with no explanation.
      if (res.deleted > 0) {
        setSuccess(`Permanently deleted “${t.title}”.`);
      } else if (res.skipped > 0) {
        setError(
          `“${t.title}” could not be deleted — it is no longer archived (it may have been ` +
          'purged by the automatic sweep, restored, or already removed). The list has been refreshed.',
        );
      } else {
        setError(`“${t.title}” was not deleted. Nothing changed.`);
      }
      setConfirmId(null);
      // Both outcomes shrink the page: `deleted` removed the row, and a
      // `skipped` row was already gone server-side (that is why it was
      // skipped), so it won't come back in the reload either.
      reloadAfterRemoval();
      refreshCounts();
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Delete failed.');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <Header title="Archive" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h1 className="text-xl font-bold tracking-tight">Archive</h1>
          <p className="text-sm text-muted-foreground tabular-nums">
            {loading && items.length === 0 ? ' ' : (
              <><span className="font-semibold text-foreground">{total.toLocaleString()}</span> archived</>
            )}
          </p>
        </div>

        <p className="text-sm text-muted-foreground max-w-[68ch]">
          Tenders that closed without a bid are archived automatically and deleted permanently
          once their countdown ends. Restore one to return it to the active tender list and stop the
          clock. Tenders archived manually or auto-discarded on score are kept indefinitely —
          they are never deleted on their own.
        </p>

        {/* Reason filter */}
        <div className="flex flex-wrap items-center gap-1.5">
          {REASON_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => changeReason(tab.key)}
              className={
                'px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ' +
                (reason === tab.key
                  ? 'bg-accent text-accent-foreground border-accent'
                  : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground')
              }
            >
              {tab.label}
            </button>
          ))}
        </div>

        {error && (
          <div className="flex items-start justify-between bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg px-4 py-3">
            <div className="flex items-start gap-2 text-sm text-red-800 dark:text-red-400">
              <XCircle size={16} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
            <button onClick={() => setError(null)} className="text-red-600 dark:text-red-400 hover:text-red-800" aria-label="Dismiss">
              <X size={16} />
            </button>
          </div>
        )}

        {success && (
          <div className="flex items-start justify-between bg-emerald-50 dark:bg-emerald-500/15 border border-emerald-200 dark:border-emerald-500/20 rounded-lg px-4 py-3">
            <div className="flex items-start gap-2 text-sm text-emerald-800 dark:text-emerald-400">
              <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
              <span>{success}</span>
            </div>
            <button onClick={() => setSuccess(null)} className="text-emerald-600 dark:text-emerald-400 hover:text-emerald-800" aria-label="Dismiss">
              <X size={16} />
            </button>
          </div>
        )}

        {loading && <LoadingSpinner />}

        {!loading && items.length === 0 && (
          <div className="flex flex-col items-center gap-2 rounded-lg border border-border bg-card py-12 text-center">
            <Archive size={28} strokeWidth={1.5} className="text-muted-foreground" />
            <p className="text-sm font-medium">Nothing archived</p>
            <p className="text-sm text-muted-foreground">
              {reason === 'all'
                ? 'Archived tenders will show up here.'
                : 'No tenders archived for this reason.'}
            </p>
          </div>
        )}

        {!loading && items.length > 0 && (
          <ul className="space-y-2">
            {items.map((t) => (
              <li
                key={t.id}
                className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border bg-card p-4"
              >
                <div className="min-w-0 flex-1 space-y-1.5">
                  <Link
                    to={`/tenders/${t.id}`}
                    className="block truncate font-medium text-foreground hover:text-accent hover:underline"
                    title={t.title}
                  >
                    {t.title}
                  </Link>
                  <p className="text-xs text-muted-foreground">
                    {portalLabel(t.portal)} · {t.tender_id}
                    {t.department ? ` · ${t.department}` : ''}
                    {t.estimated_value != null ? ` · ${formatCurrency(t.estimated_value)}` : ''}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Closed {formatDate(t.closing_date)} · Archived {formatDate(t.archived_at)}
                    {t.ai_relevance_score != null ? ` · Score ${t.ai_relevance_score}` : ''}
                  </p>
                  <ArchiveChip reason={t.archive_reason} purgeAt={t.purge_at} />
                </div>

                {/* Only the acting row is disabled — a mutation on one row must
                    not freeze the other 49 buttons on the page. */}
                {isAdminOrAbove && (
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      onClick={() => runRestore(t)}
                      disabled={busyId === t.id}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border border-border rounded-lg hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {busyId === t.id ? <Loader2 size={14} className="animate-spin" /> : <RotateCcw size={14} />}
                      Restore
                    </button>

                    {/* Irreversible — inline two-step confirm, never window.confirm. */}
                    {confirmId === t.id ? (
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-medium text-red-700 dark:text-red-400">Delete forever?</span>
                        <button
                          onClick={() => runPurge(t)}
                          disabled={busyId === t.id}
                          className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {busyId === t.id ? <Loader2 size={14} className="animate-spin" /> : null}
                          Confirm
                        </button>
                        <button
                          onClick={() => setConfirmId(null)}
                          disabled={busyId === t.id}
                          className="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setConfirmId(t.id)}
                        disabled={busyId === t.id}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-500/20 rounded-lg hover:bg-red-100 dark:hover:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        <Trash2 size={14} />
                        Delete now
                      </button>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}

        {!loading && total > PAGE_SIZE && (
          <Pagination offset={offset} limit={PAGE_SIZE} total={total} onChange={setOffset} />
        )}
      </div>
    </>
  );
}
