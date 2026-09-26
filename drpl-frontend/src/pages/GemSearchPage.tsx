import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ScanSearch, Loader2, Square, AlertTriangle, CheckCircle2, XCircle, X,
  Clock, Layers, Globe, FileText, ArrowRight,
} from 'lucide-react';
import Header from '../components/layout/Header';
import PortalBadge from '../components/ui/PortalBadge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  startCollectRun, getActiveCollectRun, getCollectCoverage, getRecentCollectRuns,
  getTenderById, openRunStream, cancelRun,
  type CollectCoverage, type CollectMode, type CollectOutcome, type RecentCollectRun,
} from '../lib/api';
import { formatCurrency, formatDate, formatDateTime } from '../lib/formatters';
import type { TenderDetail } from '../types/tender';

// ── What a sweep reports while it runs ─────────────────────────────────────
//
// A collect run is an AgentRun: same stream, same Stop, same reattach after a
// reload. This page only reads the events the collector adds (see
// collector/runbus.py) and shows tenders as they land.

interface Progress {
  portal: string;
  term: string | null;
  page: number;
  pages_done: number;
  rows_seen: number;
  in_scope: number;
  expected_total: number | null;
  /** Distinct bids examined so far. Null when the sweep does not track them. */
  rows_distinct: number | null;
}

interface Totals {
  found: number;
  duplicates: number;
  pages: number;
  enriched: number;
}

type Phase = 'idle' | 'starting' | 'running' | 'stopping';

const RUN_KEY = 'drpl_active_collect_run';
const EMPTY_TOTALS: Totals = { found: 0, duplicates: 0, pages: 0, enriched: 0 };

function modeLabel(mode: unknown): string {
  if (mode === 'ministry') return 'Complete sweep';
  if (mode === 'full') return 'Full portal audit';
  return 'Quick search';
}

const COLLECT_MODES: CollectMode[] = ['incremental', 'ministry', 'full'];

function asMode(value: unknown): CollectMode | null {
  return COLLECT_MODES.includes(value as CollectMode) ? (value as CollectMode) : null;
}

function statusTone(status: string): 'success' | 'destructive' | 'warning' | 'secondary' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'destructive';
  if (status === 'cancelled') return 'warning';
  return 'secondary';
}

export default function GemSearchPage() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [runId, setRunId] = useState<string | null>(null);
  const [runMode, setRunMode] = useState<CollectMode>('incremental');
  const [runIsMine, setRunIsMine] = useState(true);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [totals, setTotals] = useState<Totals>(EMPTY_TOTALS);
  const [landed, setLanded] = useState<TenderDetail[]>([]);
  const [notices, setNotices] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [finished, setFinished] = useState<{ status: string; summary?: string } | null>(null);
  const [outcome, setOutcome] = useState<CollectOutcome | null>(null);
  const [coverage, setCoverage] = useState<CollectCoverage | null>(null);
  const [recent, setRecent] = useState<RecentCollectRun[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const seenIds = useRef<Set<number>>(new Set());

  const refreshSidebar = useCallback(async () => {
    const [cov, runs] = await Promise.all([
      getCollectCoverage('gem').catch(() => null),
      getRecentCollectRuns(8).catch(() => []),
    ]);
    if (cov) setCoverage(cov);
    setRecent(runs);
  }, []);

  // Newly ingested rows are fetched by id and shown at the top as they land.
  const appendTenders = useCallback(async (ids: number[]) => {
    const fresh = ids.filter((id) => !seenIds.current.has(id));
    fresh.forEach((id) => seenIds.current.add(id));
    if (!fresh.length) return;
    const rows = await Promise.all(fresh.map((id) => getTenderById(id).catch(() => null)));
    const ok = rows.filter((r): r is TenderDetail => !!r);
    if (ok.length) setLanded((prev) => [...ok, ...prev]);
  }, []);

  // ── The stream ─────────────────────────────────────────────────────────
  const consume = useCallback(async (id: string) => {
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase('running');
    setRunId(id);
    localStorage.setItem(RUN_KEY, id);

    try {
      const res = await openRunStream(id, undefined, controller.signal);
      if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() ?? '';
        for (const chunk of chunks) {
          let name = '';
          let data: any = {};
          for (const line of chunk.split('\n')) {
            if (line.startsWith('event:')) name = line.slice(6).trim();
            else if (line.startsWith('data:')) {
              try { data = JSON.parse(line.slice(5).trim()); } catch { data = {}; }
            }
          }
          if (!name) continue;
          switch (name) {
            case 'collect_started': {
              const m = asMode(data.mode);
              if (m) setRunMode(m);
              break;
            }
            case 'collect_progress':
              setProgress({
                portal: data.portal, term: data.term ?? null, page: data.page ?? 0,
                pages_done: data.pages_done ?? 0, rows_seen: data.rows_seen ?? 0,
                in_scope: data.in_scope ?? 0, expected_total: data.expected_total ?? null,
                rows_distinct: data.rows_distinct ?? null,
              });
              setTotals((t) => ({ ...t, pages: Math.max(t.pages, data.pages_done ?? 0) }));
              break;
            case 'tenders_ingested':
              setTotals((t) => ({
                ...t, found: t.found + (data.new ?? 0), duplicates: t.duplicates + (data.duplicates ?? 0),
              }));
              if (Array.isArray(data.ids) && data.ids.length) void appendTenders(data.ids);
              break;
            case 'enriched':
              setTotals((t) => ({ ...t, enriched: t.enriched + (data.attempted ?? data.succeeded ?? 0) }));
              break;
            case 'drift':
              setNotices((n) => [...n,
                `${String(data.portal || 'gem').toUpperCase()}: "${data.field}" was read on ${Math.round((data.fill_rate ?? 0) * 100)}% of rows (usually ${Math.round((data.baseline ?? 0) * 100)}%). The portal may have changed its layout.`]);
              break;
            case 'portal_done':
              // Only `ministry` and `full` enumerate something they can
              // measure themselves against, so only they send coverage. An
              // incremental sweep stops on known ground by design and omits
              // it -- recording the absence is the point, because "23% of the
              // corpus" and "we did not measure" are different answers and
              // only the sweep knows which one it has.
              setOutcome({
                portal: data.portal, mode: data.mode, status: data.status,
                coverage: data.coverage, complete: data.complete,
                rows_distinct: data.rows_distinct, pages_failed: data.pages_failed,
                final_total: data.final_total,
              });
              break;
            case 'portal_failed':
              setError(`${String(data.portal || 'gem').toUpperCase()}: ${data.error ?? 'the sweep failed'}`);
              break;
            case 'ingest_failed':
              setError(`${data.count} tender(s) could not be saved and will be picked up on the next search.`);
              break;
            case 'error':
              setError(data.message ?? 'The search failed.');
              break;
            case 'run_done':
              setProgress(null);
              setFinished({ status: data.status ?? 'completed', summary: data.summary?.note });
              if (data.status === 'failed') setError((e) => e ?? 'The search failed.');
              break;
            default:
              break;
          }
        }
      }
    } catch (e: any) {
      if (e?.name !== 'AbortError') setError(e?.message ?? 'Lost the connection to the search.');
    } finally {
      setPhase('idle');
      setRunId(null);
      abortRef.current = null;
      localStorage.removeItem(RUN_KEY);
      void refreshSidebar();
    }
  }, [appendTenders, refreshSidebar]);

  // Reattach: this browser's pointer first, then the server (another device,
  // a cleared cache, or a colleague's sweep -- there is only ever one).
  useEffect(() => {
    let cancelled = false;
    void refreshSidebar();
    (async () => {
      const local = localStorage.getItem(RUN_KEY);
      let id = local;
      const active = await getActiveCollectRun();
      if (active) {
        id = active.id;
        setRunIsMine(active.mine);
        const m = asMode(active.params?.mode);
        if (m) setRunMode(m);
      }
      if (id && !cancelled) void consume(id);
    })();
    return () => {
      cancelled = true;
      abortRef.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const start = async (mode: CollectMode) => {
    setError(null);
    setNotices([]);
    setFinished(null);
    setOutcome(null);
    setTotals(EMPTY_TOTALS);
    setLanded([]);
    seenIds.current = new Set();
    setRunMode(mode);
    setRunIsMine(true);
    setPhase('starting');
    try {
      const { run_id } = await startCollectRun({ portals: ['gem_full'], mode });
      await consume(run_id);
    } catch (e: any) {
      if (e?.status === 409 && e?.existingRunId) {
        setRunIsMine(false);
        void consume(e.existingRunId);
        return;
      }
      setPhase('idle');
      setError(
        e?.status === 503
          ? 'The search worker is not reachable right now. Try again in a minute.'
          : e?.status === 403
            ? 'Your role does not have access to GeM Search.'
            : (e?.message ?? 'Could not start the search.'),
      );
    }
  };

  const stop = async () => {
    if (!runId) return;
    setPhase('stopping');
    try {
      await cancelRun(runId);
    } catch { /* the abort below still happens */ }
    abortRef.current?.abort();
  };

  const busy = phase !== 'idle';
  const pagesTotal = progress?.expected_total ? Math.ceil(progress.expected_total / 10) : null;
  // Distinct bids examined over the portal's own total, whenever the sweep
  // reports one and is not on a term pass (a term's total is that term's, not
  // the corpus's). Pages-done over pages-expected is only equivalent while a
  // sweep reads each page once: a complete sweep makes several convergence
  // passes over the same pages, so the page ratio would sit pegged at 100%
  // for most of the run while the distinct count is what is converging.
  const examined = progress && !progress.term ? progress.rows_distinct : null;
  const pct = progress?.expected_total
    ? examined != null
      ? Math.min(100, Math.round((examined / progress.expected_total) * 100))
      : pagesTotal
        ? Math.min(100, Math.round((progress.pages_done / pagesTotal) * 100))
        : null
    : null;

  // What the finished sweep is entitled to claim. The third case is the one
  // worth the words: an incremental sweep answers "what is new?" and stops on
  // known ground, so it has not seen the rest of the portal. Saying nothing
  // is how that gets read as "these are all of them".
  const claim = !outcome
    ? null
    : outcome.complete
      ? `Complete - examined ${(outcome.rows_distinct ?? 0).toLocaleString()} of ${(outcome.final_total ?? 0).toLocaleString()} live bids ${outcome.mode === 'ministry' ? 'for this ministry' : 'on the portal'}.`
      : outcome.coverage != null
        ? `Partial - covered ${Math.round(outcome.coverage * 100)}% of the live list${outcome.pages_failed ? `, ${outcome.pages_failed} page(s) failed` : ''}; some tenders were not examined.`
        : 'Newest bids only - this search stops once it recognises what it has already collected, so it does not cover the whole portal.';
  // Amber is for a sweep that measured its coverage and fell short. An
  // incremental sweep measured nothing, which is a note rather than a
  // warning -- colouring every quick search amber would train people to
  // ignore the colour by the time it means something.
  const claimIsWarning = !!outcome && !outcome.complete && outcome.coverage != null;

  return (
    <>
      <Header title="GeM Search" subtitle="Pull live Ministry of Railways bids from GeM into your tender list" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {/* Page head */}
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div>
            <p className="text-xs font-extrabold uppercase tracking-[0.14em] text-emerald-700 dark:text-emerald-400">One click</p>
            <h1 className="mt-1 text-2xl font-extrabold tracking-tight">Search GeM for railway tenders</h1>
          </div>
          {coverage?.available && (
            <p className="text-sm text-muted-foreground tabular-nums">
              <span className="font-semibold text-foreground">{(coverage.tenders_seen ?? 0).toLocaleString()}</span> GeM tenders collected so far
            </p>
          )}
        </div>

        <p className="max-w-[72ch] text-sm text-muted-foreground">
          The server visits GeM, keeps the bids whose ministry is Indian Railways, reads each bid
          document for the value, EMD and eligibility, and adds them to <Link to="/tenders" className="font-medium text-emerald-700 underline-offset-4 hover:underline dark:text-emerald-400">Tenders</Link>.
          Rows appear below as they land. You can leave the page; the search carries on.
        </p>

        {/* Controls */}
        <Card>
          <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-wrap items-center gap-2">
              {!busy ? (
                <>
                  <Button variant="accent" size="lg" onClick={() => start('incremental')}>
                    <ScanSearch />
                    Search GeM
                  </Button>
                  <Button variant="outline" size="lg" onClick={() => start('ministry')} title="Ask GeM's own ministry filter for every live Railways bid and keep going until the count matches the portal's. About 13 minutes, and a third of the portal traffic of a full audit.">
                    <Layers />
                    Complete sweep
                  </Button>
                  <Button variant="ghost" size="lg" onClick={() => start('full')} title="Read every live bid on the portal, not only the ones GeM has labelled Railways — the only way to catch a mislabelled one. Slower and much heavier; run it occasionally.">
                    <Globe />
                    Full portal audit
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="accent" size="lg" disabled>
                    <Loader2 className="animate-spin" />
                    {phase === 'starting' ? 'Starting…' : phase === 'stopping' ? 'Stopping…' : totals.found > 0 ? `${totals.found} new found` : 'Searching…'}
                  </Button>
                  <Button variant="outline" size="lg" onClick={stop} disabled={phase !== 'running'}>
                    <Square />
                    Stop
                  </Button>
                </>
              )}
            </div>
            <div className="text-sm text-muted-foreground">
              {busy ? (
                <span className="inline-flex items-center gap-2">
                  <Badge variant="secondary">{modeLabel(runMode)}</Badge>
                  {!runIsMine && <span>started by a colleague — you are watching it</span>}
                </span>
              ) : (
                <span>Quick search finds what is new in about a minute. Complete sweep covers every live railway bid and reports its coverage. The audit reads the whole portal.</span>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Live progress */}
        {busy && (
          <div className="rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="inline-flex items-center gap-2">
                <Loader2 size={14} className="animate-spin" />
                {progress
                  ? examined != null && progress.expected_total
                    ? <>{progress.portal.toUpperCase()} · page {progress.page.toLocaleString()} · {examined.toLocaleString()} of ~{progress.expected_total.toLocaleString()} bids examined</>
                    : <>{progress.portal.toUpperCase()}{progress.term ? ` · "${progress.term}"` : ''} · page {progress.page.toLocaleString()}{pagesTotal ? ` of ~${pagesTotal.toLocaleString()}` : ''}</>
                  : 'Connecting to the portal…'}
              </span>
              <span className="tabular-nums">
                {totals.found} new · {totals.duplicates} already known · {totals.pages.toLocaleString()} pages
              </span>
            </div>
            {pct !== null && (
              <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-border">
                <div className="h-full rounded-full bg-emerald-500 transition-all duration-500" style={{ width: `${pct}%` }} />
              </div>
            )}
          </div>
        )}

        {/* Outcome banners */}
        {error && (
          <div className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-500/20 dark:bg-red-500/15 dark:text-red-300">
            <XCircle size={16} className="mt-0.5 shrink-0" />
            <span className="flex-1">{error}</span>
            <button onClick={() => setError(null)} className="shrink-0 opacity-70 hover:opacity-100" aria-label="Dismiss"><X size={14} /></button>
          </div>
        )}
        {finished && !error && (
          <div className={claimIsWarning
            ? 'flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/20 dark:bg-amber-500/15 dark:text-amber-300'
            : 'flex items-start gap-3 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 dark:border-emerald-500/20 dark:bg-emerald-500/15 dark:text-emerald-300'}>
            {claimIsWarning
              ? <AlertTriangle size={16} className="mt-0.5 shrink-0" />
              : <CheckCircle2 size={16} className="mt-0.5 shrink-0" />}
            <span className="flex-1">
              {finished.status === 'cancelled' ? 'Search stopped. ' : 'Search finished. '}
              {totals.found} new tender{totals.found === 1 ? '' : 's'} added, {totals.duplicates} already in your list
              {totals.pages ? `, ${totals.pages.toLocaleString()} pages read` : ''}.
              {claim ? ` ${claim}` : ''}
            </span>
            <button onClick={() => setFinished(null)} className="shrink-0 opacity-70 hover:opacity-100" aria-label="Dismiss"><X size={14} /></button>
          </div>
        )}
        {notices.map((n, i) => (
          <div key={i} className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/20 dark:bg-amber-500/15 dark:text-amber-300">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <span>{n}</span>
          </div>
        ))}

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          {/* Tenders landed this run */}
          <div className="space-y-3">
            <div className="flex items-baseline justify-between">
              <h2 className="text-sm font-bold uppercase tracking-wide text-muted-foreground">Added in this search</h2>
              {landed.length > 0 && (
                <Link to="/tenders" className="inline-flex items-center gap-1 text-sm font-medium text-emerald-700 hover:underline dark:text-emerald-400">
                  Open Tenders <ArrowRight size={14} />
                </Link>
              )}
            </div>
            {landed.length === 0 ? (
              <div className="rounded-xl border border-border bg-card p-8 text-center">
                <FileText className="mx-auto mb-2 text-muted-foreground" size={22} strokeWidth={1.5} />
                <p className="text-sm text-muted-foreground">
                  {busy ? 'Nothing new yet — bids already in your list are skipped.' : 'New tenders from the next search will appear here.'}
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto rounded-xl border border-border bg-card">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border bg-card text-xs uppercase text-muted-foreground">
                      <th className="px-4 py-2 text-left">Tender</th>
                      <th className="px-4 py-2 text-left">Organisation</th>
                      <th className="px-4 py-2 text-right">Worth</th>
                      <th className="px-4 py-2 text-right">EMD</th>
                      <th className="px-4 py-2 text-left">Closes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {landed.map((t) => (
                      <tr key={t.id} className="border-b border-border last:border-0 hover:bg-muted/40">
                        <td className="max-w-md px-4 py-2.5">
                          <Link to={`/tenders/${t.id}`} className="font-medium text-foreground hover:underline">
                            {t.title}
                          </Link>
                          <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                            <PortalBadge portal={t.portal} />
                            <span className="font-mono">{t.tender_id}</span>
                          </div>
                        </td>
                        <td className="max-w-xs truncate px-4 py-2.5 text-muted-foreground" title={t.organisation ?? ''}>{t.organisation || t.department || '—'}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums">{t.estimated_value ? formatCurrency(t.estimated_value) : '—'}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums">{t.emd_amount != null ? (t.emd_amount === 0 ? 'Nil' : formatCurrency(t.emd_amount)) : '—'}</td>
                        <td className="px-4 py-2.5 whitespace-nowrap text-muted-foreground">{formatDate(t.closing_date)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Coverage + history */}
          <div className="space-y-3">
            <h2 className="text-sm font-bold uppercase tracking-wide text-muted-foreground">Last searches</h2>
            <Card>
              <CardContent className="p-0">
                {recent.length === 0 ? (
                  <p className="p-5 text-sm text-muted-foreground">No searches yet.</p>
                ) : (
                  <ul className="divide-y divide-border">
                    {recent.map((r) => (
                      <li key={r.id} className="px-4 py-3">
                        <div className="flex items-center justify-between gap-2">
                          <span className="inline-flex items-center gap-1.5 text-sm font-medium">
                            <Clock size={13} className="text-muted-foreground" />
                            {formatDateTime(r.created_at)}
                          </span>
                          <Badge variant={statusTone(r.status)}>{r.status}</Badge>
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {modeLabel(r.params?.mode)}{r.mine ? '' : ' · colleague'}
                          {r.summary ? ` · ${r.summary}` : ''}
                          {r.error && !r.summary ? ` · ${r.error}` : ''}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
            {coverage?.available && coverage.last_sweep && (
              <p className="px-1 text-xs text-muted-foreground">
                {/*
                  Rows READ, not bids examined -- the two are the same number
                  only for a sweep that reads each page once. A complete sweep
                  repeats pages until its distinct count reaches the portal's,
                  so "examined N of M" off this row would print N above M.
                  The distinct figure is on the run's own banner above.
                */}
                Last run ({modeLabel(coverage.last_sweep.mode).toLowerCase()}): {coverage.last_sweep.rows_seen.toLocaleString()} listing rows read
                {coverage.last_sweep.expected_total ? ` against a portal total of ${coverage.last_sweep.expected_total.toLocaleString()}` : ''}.
                {coverage.last_sweep.mode === 'ministry' ? ' A complete sweep re-reads pages until the counts agree, so rows read runs higher.' : ''}
                {coverage.open_gaps ? ` ${coverage.open_gaps} bid number${coverage.open_gaps === 1 ? '' : 's'} not yet seen.` : ''}
              </p>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
