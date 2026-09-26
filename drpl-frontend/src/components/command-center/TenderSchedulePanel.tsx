/**
 * TenderSchedulePanel — read-only view of the captured NIT bidding schedule.
 *
 * Renders the verbatim BOQItem rows for a tender, grouped by schedule_name
 * (Schedule A, Schedule B, ...), so the user can sit it next to the editable
 * cost breakdown and verify the costing agent's 1:1 mirror.
 *
 * Backend contract: GET /api/tenders/{tender_id}/bidding-schedule
 */

import { useEffect, useMemo, useState } from 'react';
import { Loader2, ChevronDown, ChevronRight, FileText } from 'lucide-react';
import { getTenderBiddingSchedule, type TenderScheduleRow } from '../../lib/api';

interface TenderSchedulePanelProps {
  tenderId: number;
  /** When true, the panel starts expanded; otherwise collapsed. */
  defaultOpen?: boolean;
}

const fmtNum = (n: number | null | undefined, digits = 2): string => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString('en-IN', {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
};

const fmtInt = (n: number | null | undefined): string =>
  n === null || n === undefined ? '—' : Number(n).toLocaleString('en-IN');

export function TenderSchedulePanel({ tenderId, defaultOpen = false }: TenderSchedulePanelProps) {
  const [rows, setRows] = useState<TenderScheduleRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(defaultOpen);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getTenderBiddingSchedule(tenderId)
      .then((data) => {
        if (cancelled) return;
        setRows(data.rows || []);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : 'Failed to load tender schedule';
        setError(msg);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tenderId]);

  const grouped = useMemo(() => {
    if (!rows) return [] as Array<{ schedule: string; rows: TenderScheduleRow[] }>;
    const map = new Map<string, TenderScheduleRow[]>();
    const order: string[] = [];
    for (const r of rows) {
      const key = r.schedule_name || '?';
      if (!map.has(key)) {
        map.set(key, []);
        order.push(key);
      }
      map.get(key)!.push(r);
    }
    return order.map((s) => ({ schedule: s, rows: map.get(s)! }));
  }, [rows]);

  if (loading && !rows) {
    return (
      <div className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <Loader2 size={12} className="mr-1.5 inline animate-spin" />
        Loading captured NIT schedule…
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/15 px-3 py-2 text-xs text-red-800 dark:text-red-400">
        Failed to load NIT schedule: {error}
      </div>
    );
  }

  if (!rows || rows.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        No NIT bidding schedule has been captured for this tender yet. Run the tender
        analyzer first — it auto-extracts the Schedule of Items table from the NIT PDF.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-accent/20 bg-accent/10/30">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold text-accent"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <FileText size={14} />
        Tender Bidding Schedule ({rows.length} row{rows.length === 1 ? '' : 's'},{' '}
        {grouped.length} schedule{grouped.length === 1 ? '' : 's'})
        <span className="ml-1 rounded-full bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium text-accent">
          read-only · verbatim from NIT
        </span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-accent/20 p-3">
          {grouped.map(({ schedule, rows: sched_rows }) => (
            <div key={schedule}>
              <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-accent">
                {schedule === '?' ? 'Other Items' : `Schedule ${schedule}`} ({sched_rows.length})
              </div>
              <div className="overflow-x-auto rounded border border-accent/20 bg-card">
                <table className="min-w-full text-[11px]">
                  <thead className="bg-accent/10 text-foreground">
                    <tr>
                      <th className="px-2 py-1.5 text-left font-semibold">#</th>
                      <th className="px-2 py-1.5 text-left font-semibold">Item Code</th>
                      <th className="px-2 py-1.5 text-left font-semibold">Description</th>
                      <th className="px-2 py-1.5 text-right font-semibold">Qty</th>
                      <th className="px-2 py-1.5 text-left font-semibold">Unit</th>
                      <th className="px-2 py-1.5 text-right font-semibold">Unit Rate</th>
                      <th className="px-2 py-1.5 text-right font-semibold">Basic Value</th>
                      <th className="px-2 py-1.5 text-right font-semibold">Escl.%</th>
                      <th className="px-2 py-1.5 text-left font-semibold">Bidding Unit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sched_rows.map((r) => (
                      <tr
                        key={r.id}
                        className={
                          r.is_tax_line
                            ? 'border-t border-orange-100 bg-orange-50/60'
                            : 'border-t border-border'
                        }
                      >
                        <td className="px-2 py-1 align-top">{r.sr_no ?? '—'}</td>
                        <td className="px-2 py-1 align-top font-mono text-[10px]">
                          {r.item_code || '—'}
                        </td>
                        <td className="px-2 py-1 align-top">
                          <div className="line-clamp-3 text-foreground" title={r.description}>
                            {r.description}
                          </div>
                          {r.is_tax_line && (
                            <span className="mt-1 inline-block rounded bg-orange-100 dark:bg-orange-500/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-orange-800 dark:text-orange-400">
                              Tax line
                            </span>
                          )}
                        </td>
                        <td className="px-2 py-1 text-right align-top">{fmtInt(r.quantity)}</td>
                        <td className="px-2 py-1 align-top">{r.unit || '—'}</td>
                        <td className="px-2 py-1 text-right align-top">
                          {fmtNum(r.estimated_rate)}
                        </td>
                        <td className="px-2 py-1 text-right align-top">
                          {fmtNum(r.basic_value)}
                        </td>
                        <td className="px-2 py-1 text-right align-top">
                          {fmtNum(r.escalation_pct ?? 0, 2)}
                        </td>
                        <td className="px-2 py-1 align-top text-muted-foreground">
                          {r.bidding_unit || '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default TenderSchedulePanel;
