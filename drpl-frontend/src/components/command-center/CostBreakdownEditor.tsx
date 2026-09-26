/**
 * CostBreakdownEditor — editable spreadsheet UI for the per-tender cost
 * breakdown produced by the costing_researcher agent.
 *
 * Loads the latest CostBreakdown for a tender, renders an editable table with
 * live total recomputation, and lets the user save edits + regenerate the XLSX
 * artifact in one click.
 *
 * Cost-only mode (default): the agent emits a single `rate` per line with
 * `profit_pct: null`. The user controls margin via three knobs:
 *   1. Default Margin % (global) — applied to every line that doesn't have
 *      its own override.
 *   2. Margin % override (per-line) — leave blank to inherit the global.
 *   3. Overhead % and GST % — applied to subtotal / pre-tax respectively.
 * Totals recompute live as the user edits any of these.
 *
 * Range mode (legacy): when an existing breakdown carries low/high rate bands
 * (rate_low AND rate_high non-null on at least one line), the editor renders
 * three rate inputs and a Low / Expected / High totals table. New breakdowns
 * from the rewritten agent never enter this mode.
 */

import { useEffect, useMemo, useState } from 'react';
import { Loader2, Save, FileSpreadsheet, FileText, AlertTriangle, Plus, Trash2, RotateCcw, ChevronDown, ChevronRight } from 'lucide-react';
import {
  getCostBreakdown,
  updateCostBreakdown,
  regenerateCostXlsx,
  regenerateCostPdf,
  saveArtifactFile,
} from '../../lib/api';
import type { CostBreakdown, CostBreakdownLine } from '../../types/command-center';
import { TenderSchedulePanel } from './TenderSchedulePanel';

interface CostBreakdownEditorProps {
  tenderId: number;
  sessionId?: number;
  /** Called after the user clicks "Regenerate XLSX" so the parent can refresh artifacts. */
  onXlsxRegenerated?: (artifactId: number) => void;
}

const SOURCE_OPTIONS = [
  { value: '', label: '—' },
  { value: 'training_data', label: '📚 Training data' },
  { value: 'tender_estimate', label: '📄 Tender estimate' },
  { value: 'web_search', label: '🌐 Web search' },
  { value: 'memory', label: '🧠 Memory' },
  { value: 'derived_estimate', label: '🧮 Derived estimate' },
  { value: 'user_override', label: '✏️ User override' },
  { value: 'needs_user_input', label: '⚠️ Needs input' },
];

const fmtMoney = (n: number | null | undefined): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
};

/** Compute amount for a given rate band (low/expected/high). */
const computeAmountAt = (line: CostBreakdownLine, band: 'low' | 'expected' | 'high'): number | null => {
  if (line.needs_input) return null;
  if (line.quantity == null) return null;
  let rate: number | null | undefined;
  if (band === 'low') rate = line.rate_low ?? line.rate;
  else if (band === 'high') rate = line.rate_high ?? line.rate;
  else rate = line.rate;
  if (rate == null) return null;
  return Number((line.quantity * rate).toFixed(2));
};

const computeAmount = (line: CostBreakdownLine): number | null => computeAmountAt(line, 'expected');

interface TotalsBand {
  subtotal: number;
  overheadAmt: number;
  marginAmt: number;
  gstAmt: number;
  grand: number;
}

interface Totals {
  low: TotalsBand;
  expected: TotalsBand;
  high: TotalsBand;
  needsInputCount: number;
  hasRange: boolean;
}

const recomputeTotals = (
  lines: CostBreakdownLine[],
  overhead: number,
  margin: number,
  gst: number,
): Totals => {
  let needsInputCount = 0;
  let hasRange = false;

  let subLow = 0, subExp = 0, subHigh = 0;
  let profitLow = 0, profitExp = 0, profitHigh = 0;

  for (const ln of lines) {
    // An annexure component's amount is a per-set cost already rolled into
    // its parent item's rate (server-side). Counting it here as well would
    // double the subtotal the moment an annexure is captured.
    if (ln.parent_boq_item_id != null) continue;
    if (ln.needs_input) { needsInputCount++; continue; }
    const aExp = ln.amount ?? computeAmountAt(ln, 'expected');
    if (aExp == null) continue;
    const aLow = ln.amount_low ?? computeAmountAt(ln, 'low') ?? aExp;
    const aHigh = ln.amount_high ?? computeAmountAt(ln, 'high') ?? aExp;
    if (aLow !== aExp || aHigh !== aExp) hasRange = true;

    subLow += aLow; subExp += aExp; subHigh += aHigh;
    const linePct = ln.profit_pct ?? margin;
    if (ln.profit_pct != null) hasRange = true;
    profitLow += aLow * (linePct / 100);
    profitExp += aExp * (linePct / 100);
    profitHigh += aHigh * (linePct / 100);
  }

  const band = (subtotal: number, profitSum: number): TotalsBand => {
    const overheadAmt = subtotal * (overhead / 100);
    const subWithOverhead = subtotal + overheadAmt;
    const marginOnOverhead = overheadAmt * (margin / 100);
    const marginAmt = profitSum + marginOnOverhead;
    const preTax = subWithOverhead + marginAmt;
    const gstAmt = preTax * (gst / 100);
    const grand = preTax + gstAmt;
    return {
      subtotal: Number(subtotal.toFixed(2)),
      overheadAmt: Number(overheadAmt.toFixed(2)),
      marginAmt: Number(marginAmt.toFixed(2)),
      gstAmt: Number(gstAmt.toFixed(2)),
      grand: Number(grand.toFixed(2)),
    };
  };

  return {
    low: band(subLow, profitLow),
    expected: band(subExp, profitExp),
    high: band(subHigh, profitHigh),
    needsInputCount,
    hasRange,
  };
};

export default function CostBreakdownEditor({
  tenderId,
  sessionId,
  onXlsxRegenerated,
}: CostBreakdownEditorProps) {
  const [breakdown, setBreakdown] = useState<CostBreakdown | null>(null);
  const [lines, setLines] = useState<CostBreakdownLine[]>([]);
  const [overhead, setOverhead] = useState(10);
  const [margin, setMargin] = useState(15);
  const [gst, setGst] = useState(18);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const [decompositionOpen, setDecompositionOpen] = useState(false);
  // Collapsed schedule groups (by group label). For large tenders we collapse
  // all but the first group on load so 300–500 rows don't all mount at once.
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());

  // Load breakdown
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getCostBreakdown(tenderId)
      .then((b) => {
        if (cancelled) return;
        setBreakdown(b);
        setLines(b.lines);
        setOverhead(b.overhead_percent);
        setMargin(b.margin_percent);
        setGst(b.gst_percent);
        setDirty(false);
        setSavedAt(b.updated_at ? new Date(b.updated_at) : null);
        // Auto-collapse all but the first schedule group on large breakdowns
        // to keep the initial render snappy (the user expands as needed).
        if ((b.lines?.length ?? 0) > 200) {
          const labels: string[] = [];
          const seen = new Set<string>();
          for (const ln of b.lines) {
            const sec = (ln.schedule_section || '').trim() || '—';
            if (!seen.has(sec)) { seen.add(sec); labels.push(sec); }
          }
          setCollapsedGroups(new Set(labels.slice(1)));
        } else {
          setCollapsedGroups(new Set());
        }
      })
      .catch((err) => {
        if (cancelled) return;
        if (err?.response?.status === 404) {
          setError('No cost breakdown yet. Ask the costing agent to cost this tender first.');
        } else {
          setError(err?.response?.data?.detail || err?.message || 'Failed to load cost breakdown');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [tenderId]);

  // Live totals
  const totals = useMemo(
    () => recomputeTotals(lines, overhead, margin, gst),
    [lines, overhead, margin, gst],
  );

  const updateLine = (idx: number, patch: Partial<CostBreakdownLine>) => {
    setLines((prev) => {
      const next = [...prev];
      const merged = { ...next[idx], ...patch };
      // Recompute amounts whenever qty or any rate changes
      const rateChanged = 'quantity' in patch || 'rate' in patch || 'rate_low' in patch || 'rate_high' in patch;
      if (rateChanged) {
        merged.amount = computeAmountAt(merged, 'expected');
        merged.amount_low = merged.rate_low != null ? computeAmountAt(merged, 'low') : null;
        merged.amount_high = merged.rate_high != null ? computeAmountAt(merged, 'high') : null;
      }
      // Recompute per-line profit amounts whenever profit_pct or any amount changes
      if (rateChanged || 'profit_pct' in patch) {
        const pct = merged.profit_pct;
        if (pct != null) {
          merged.profit_amount = merged.amount != null ? Number((merged.amount * pct / 100).toFixed(2)) : null;
          merged.profit_amount_low = merged.amount_low != null ? Number((merged.amount_low * pct / 100).toFixed(2)) : null;
          merged.profit_amount_high = merged.amount_high != null ? Number((merged.amount_high * pct / 100).toFixed(2)) : null;
        }
      }
      // Phase 3b — recompute tender_amount + margin when tender_rate or qty changes
      const tenderChanged = 'tender_rate' in patch || 'quantity' in patch;
      if (tenderChanged) {
        if (merged.tender_rate != null && merged.quantity != null) {
          merged.tender_amount = Number((merged.tender_rate * merged.quantity).toFixed(2));
        } else if (!('tender_rate' in patch) || patch.tender_rate == null) {
          merged.tender_amount = null;
        }
      }
      // Recompute margin per band whenever tender_amount or any cost-band amount changes
      if (rateChanged || tenderChanged) {
        if (merged.tender_amount != null) {
          merged.margin_amount = merged.amount != null
            ? Number((merged.tender_amount - merged.amount).toFixed(2)) : null;
          // High cost band → low margin and vice-versa
          merged.margin_amount_low = merged.amount_high != null
            ? Number((merged.tender_amount - merged.amount_high).toFixed(2)) : null;
          merged.margin_amount_high = merged.amount_low != null
            ? Number((merged.tender_amount - merged.amount_low).toFixed(2)) : null;
          merged.margin_pct = (merged.margin_amount != null && merged.tender_amount)
            ? Number((merged.margin_amount / merged.tender_amount * 100).toFixed(2)) : null;
        } else {
          merged.margin_amount = merged.margin_amount_low = merged.margin_amount_high = null;
          merged.margin_pct = null;
        }
      }
      // Toggling needs_input clears all numeric fields
      if (patch.needs_input === true) {
        merged.rate = merged.rate_low = merged.rate_high = null;
        merged.amount = merged.amount_low = merged.amount_high = null;
        merged.profit_amount = merged.profit_amount_low = merged.profit_amount_high = null;
        merged.margin_amount = merged.margin_amount_low = merged.margin_amount_high = null;
        merged.margin_pct = null;
      }
      // Auto-mark needs_input false when the user enters a rate
      if ('rate' in patch && patch.rate != null && merged.needs_input) {
        merged.needs_input = false;
        if (!merged.rate_source || merged.rate_source === 'needs_user_input') {
          merged.rate_source = 'user_override';
        }
      }
      next[idx] = merged;
      return next;
    });
    setDirty(true);
  };

  const addLine = () => {
    const nextSr = lines.length > 0 ? Math.max(...lines.map((l) => l.sr_no)) + 1 : 1;
    setLines((prev) => [
      ...prev,
      {
        sr_no: nextSr,
        description: '',
        category: null,
        quantity: null,
        unit: null,
        rate: null,
        amount: null,
        rate_low: null,
        rate_high: null,
        amount_low: null,
        amount_high: null,
        profit_pct: null,
        profit_amount: null,
        profit_amount_low: null,
        profit_amount_high: null,
        // Phase 3b — margin / schedule fields default to null on user-added rows
        tender_rate: null,
        tender_amount: null,
        margin_amount_low: null,
        margin_amount: null,
        margin_amount_high: null,
        margin_pct: null,
        schedule_section: null,
        cost_buildup_note: null,
        rate_source: 'user_override',
        source_ref: null,
        confidence: 'medium',
        needs_input: false,
        notes: null,
      },
    ]);
    setDirty(true);
  };

  const removeLine = (idx: number) => {
    setLines((prev) => prev.filter((_, i) => i !== idx));
    setDirty(true);
  };

  const handleSave = async () => {
    if (!breakdown) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateCostBreakdown(tenderId, {
        lines,
        overhead_percent: overhead,
        margin_percent: margin,
        gst_percent: gst,
      });
      setBreakdown(updated);
      setLines(updated.lines);
      setDirty(false);
      setSavedAt(new Date());
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const [regeneratingSingle, setRegeneratingSingle] = useState(false);
  const handleRegenerate = async (singleSheet?: boolean) => {
    if (!breakdown) return;
    if (dirty) {
      await handleSave();
    }
    const setBusy = singleSheet ? setRegeneratingSingle : setRegenerating;
    setBusy(true);
    setError(null);
    try {
      const info = await regenerateCostXlsx(tenderId, sessionId, { singleSheet });
      onXlsxRegenerated?.(info.artifact_id);
      // Actually download the freshly-generated workbook — regenerate alone
      // only refreshed the artifact panel; the button label promises a file.
      // The regenerate response carries a link to the workbook it just built,
      // so the browser fetches the bytes once, direct from storage, instead of
      // asking the backend to pull them back out and re-stream them.
      await saveArtifactFile(
        info.artifact_id,
        info.file_name || `cost_tender_${tenderId}.xlsx`,
        info.download_url,
      );
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'XLSX download failed');
    } finally {
      setBusy(false);
    }
  };

  const [regeneratingPdf, setRegeneratingPdf] = useState(false);
  const handleRegeneratePdf = async () => {
    if (!breakdown) return;
    if (dirty) {
      await handleSave();
    }
    setRegeneratingPdf(true);
    setError(null);
    try {
      const info = await regenerateCostPdf(tenderId, sessionId);
      onXlsxRegenerated?.(info.artifact_id);
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'PDF regeneration failed');
    } finally {
      setRegeneratingPdf(false);
    }
  };

  const toggleGroup = (label: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  };

  const handleResetPercents = () => {
    if (!breakdown) return;
    setOverhead(breakdown.overhead_percent);
    setMargin(breakdown.margin_percent);
    setGst(breakdown.gst_percent);
    setDirty(true);
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-4">
        <Loader2 size={16} className="animate-spin" />
        Loading cost breakdown…
      </div>
    );
  }

  if (error && !breakdown) {
    return (
      <div className="rounded-lg border border-amber-200 dark:border-amber-500/20 bg-amber-50 dark:bg-amber-500/15 p-4 text-sm text-amber-900">
        <div className="flex items-start gap-2">
          <AlertTriangle size={16} className="mt-0.5 flex-shrink-0" />
          <div>{error}</div>
        </div>
      </div>
    );
  }

  if (!breakdown) return null;

  const decomposition = breakdown.manpower_resource_analysis ?? [];
  // Range columns only render when at least one line carries an explicit
  // rate band (rate_low + rate_high). A bare profit_pct on a line does NOT
  // trigger range mode — that's a margin override, displayed in its own column.
  const showRangeColumns = totals.hasRange ||
    lines.some((ln) => ln.rate_low != null && ln.rate_high != null);
  // Phase 3b — margin-analysis mode: render Tender Rate + Margin columns,
  // group rows by schedule_section, surface the strategic summary at the top.
  const showMarginColumns = lines.some(
    (ln) => ln.tender_rate != null || ln.tender_amount != null,
  );
  const strategicSummary = breakdown.strategic_summary ?? null;
  const costAssumptions = breakdown.cost_assumptions ?? [];

  // Bucket lines by schedule_section for grouped rendering when margin mode is active.
  const lineGroups: { schedule: string; rows: { idx: number; line: CostBreakdownLine }[] }[] = [];
  if (showMarginColumns) {
    const seen: Record<string, number> = {};
    lines.forEach((ln, idx) => {
      const sec = (ln.schedule_section || '').trim() || '—';
      if (!(sec in seen)) {
        seen[sec] = lineGroups.length;
        lineGroups.push({ schedule: sec, rows: [] });
      }
      lineGroups[seen[sec]].rows.push({ idx, line: ln });
    });
  }

  return (
    <div className="space-y-4">
      {/* Header — version, dirty marker, action buttons */}
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2">
        <div className="text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">v{breakdown.version}</span>
          {breakdown.created_by_agent && <> · drafted by {breakdown.created_by_agent}</>}
          {savedAt && !dirty && <> · saved {savedAt.toLocaleTimeString()}</>}
          {dirty && <span className="ml-2 rounded-full bg-amber-100 dark:bg-amber-500/20 px-2 py-0.5 text-amber-800 dark:text-amber-400">unsaved</span>}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleSave}
            disabled={!dirty || saving}
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-accent/90 disabled:opacity-50"
          >
            {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            Save
          </button>
          <button
            onClick={() => handleRegenerate(false)}
            disabled={regenerating || regeneratingSingle || regeneratingPdf || saving}
            className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-emerald-700 disabled:opacity-50"
            title="Download as IREPS-format XLSX (one sheet per Schedule + Summary)"
          >
            {regenerating ? <Loader2 size={13} className="animate-spin" /> : <FileSpreadsheet size={13} />}
            Download XLSX
          </button>
          <button
            onClick={() => handleRegenerate(true)}
            disabled={regenerating || regeneratingSingle || regeneratingPdf || saving}
            className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-700 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-emerald-800 disabled:opacity-50"
            title="Download single-sheet XLSX — every schedule stacked into one tab, mirroring the NIT exactly"
          >
            {regeneratingSingle ? <Loader2 size={13} className="animate-spin" /> : <FileSpreadsheet size={13} />}
            Single-sheet XLSX
          </button>
          <button
            onClick={handleRegeneratePdf}
            disabled={regeneratingPdf || regenerating || regeneratingSingle || saving}
            className="inline-flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-rose-700 disabled:opacity-50"
            title="Download as PDF (NIT-mirror layout, ready for procurement / DSC signing)"
          >
            {regeneratingPdf ? <Loader2 size={13} className="animate-spin" /> : <FileText size={13} />}
            Download PDF
          </button>
        </div>
      </div>

      {/* NIT bidding-schedule reference panel — shown above the editor so the
          user can verify their costing 1:1-mirrors the captured NIT layout. */}
      <TenderSchedulePanel tenderId={tenderId} defaultOpen={false} />

      {/* Banners */}
      {totals.needsInputCount > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 dark:border-amber-500/20 bg-amber-50 dark:bg-amber-500/15 px-3 py-2 text-xs text-amber-900">
          <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
          <div>
            <strong>{totals.needsInputCount} line{totals.needsInputCount === 1 ? '' : 's'} need your input.</strong>{' '}
            They are excluded from the totals. Fill in the rate to include them.
          </div>
        </div>
      )}

      {/* Phase 3b — Strategic Summary panel (Sheet 1 equivalent) */}
      {strategicSummary && (
        <div className="space-y-3 rounded-lg border border-accent/20 bg-accent/5 p-3">
          {strategicSummary.tender_snapshot && (
            <div>
              <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-accent">Tender Snapshot</div>
              <div className="grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
                {strategicSummary.tender_snapshot.tender_no && (
                  <div><span className="font-medium text-foreground">Tender No.</span> {strategicSummary.tender_snapshot.tender_no}</div>
                )}
                {strategicSummary.tender_snapshot.scope_one_liner && (
                  <div className="sm:col-span-2"><span className="font-medium text-foreground">Scope</span> {strategicSummary.tender_snapshot.scope_one_liner}</div>
                )}
                {strategicSummary.tender_snapshot.tender_value_inr != null && (
                  <div><span className="font-medium text-foreground">Tender Value</span> {fmtMoney(strategicSummary.tender_snapshot.tender_value_inr)}</div>
                )}
                {strategicSummary.tender_snapshot.period && (
                  <div><span className="font-medium text-foreground">Period</span> {strategicSummary.tender_snapshot.period}</div>
                )}
                {strategicSummary.tender_snapshot.emd_inr != null && (
                  <div><span className="font-medium text-foreground">EMD</span> {fmtMoney(strategicSummary.tender_snapshot.emd_inr)}</div>
                )}
                {strategicSummary.tender_snapshot.performance_guarantee && (
                  <div><span className="font-medium text-foreground">Performance Guarantee</span> {strategicSummary.tender_snapshot.performance_guarantee}</div>
                )}
                {strategicSummary.tender_snapshot.bid_validity_days != null && (
                  <div><span className="font-medium text-foreground">Bid Validity</span> {strategicSummary.tender_snapshot.bid_validity_days} days</div>
                )}
                {strategicSummary.tender_snapshot.penalty_cap_pct_of_contract != null && (
                  <div><span className="font-medium text-foreground">Penalty Cap</span> {strategicSummary.tender_snapshot.penalty_cap_pct_of_contract}%</div>
                )}
                {strategicSummary.tender_snapshot.depots_or_locations && strategicSummary.tender_snapshot.depots_or_locations.length > 0 && (
                  <div className="sm:col-span-2"><span className="font-medium text-foreground">Depots / Locations</span> {strategicSummary.tender_snapshot.depots_or_locations.join(', ')}</div>
                )}
                {strategicSummary.tender_snapshot.min_eligibility && strategicSummary.tender_snapshot.min_eligibility.length > 0 && (
                  <div className="sm:col-span-2"><span className="font-medium text-foreground">Min. Eligibility</span> {strategicSummary.tender_snapshot.min_eligibility.join('; ')}</div>
                )}
              </div>
            </div>
          )}

          {strategicSummary.schedule_breakdown && strategicSummary.schedule_breakdown.length > 0 && (
            <div>
              <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-accent">Schedule-wise Profitability</div>
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="text-left font-normal">Schedule</th>
                    <th className="text-right font-normal">Tender Value</th>
                    <th className="text-right font-normal">Estimated Cost</th>
                    <th className="text-right font-normal">Gross Margin</th>
                    <th className="text-right font-normal">GM %</th>
                  </tr>
                </thead>
                <tbody>
                  {strategicSummary.schedule_breakdown.map((s, i) => {
                    const tv = s.tender_value_inr ?? 0;
                    const ec = s.estimated_cost_inr ?? 0;
                    const gm = s.gross_margin_inr ?? (tv - ec);
                    const gp = s.gross_margin_pct ?? (tv ? (gm / tv) * 100 : 0);
                    return (
                      <tr key={i} className="border-t border-accent/20">
                        <td className="py-0.5">{s.schedule || `Schedule #${i + 1}`}</td>
                        <td className="py-0.5 text-right">{fmtMoney(tv)}</td>
                        <td className="py-0.5 text-right">{fmtMoney(ec)}</td>
                        <td className="py-0.5 text-right">{fmtMoney(gm)}</td>
                        <td className="py-0.5 text-right">{Number(gp).toFixed(1)}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {strategicSummary.key_observations && strategicSummary.key_observations.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-accent">Key Observations &amp; Strategy</div>
              <ul className="list-disc space-y-0.5 pl-5 text-xs text-foreground">
                {strategicSummary.key_observations.map((o, i) => <li key={i}>{o}</li>)}
              </ul>
            </div>
          )}

          {strategicSummary.recommended_bid_strategy && (
            <div className="rounded-md bg-accent/15/60 px-2 py-1.5 text-xs italic text-accent">
              <span className="font-semibold">Recommended bid:</span> {strategicSummary.recommended_bid_strategy}
            </div>
          )}
        </div>
      )}

      {/* Manpower & Resource Decomposition (collapsible) */}
      {decomposition.length > 0 && (
        <div className="rounded-lg border border-border">
          <button
            onClick={() => setDecompositionOpen((v) => !v)}
            className="flex w-full items-center justify-between gap-2 px-3 py-2 text-xs font-semibold text-foreground hover:bg-muted/40"
          >
            <span className="flex items-center gap-1.5">
              {decompositionOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              Manpower &amp; Resource Decomposition ({decomposition.length} bucket{decomposition.length === 1 ? '' : 's'})
            </span>
            <span className="text-[10px] font-normal text-muted-foreground">agent's audit trail</span>
          </button>
          {decompositionOpen && (
            <div className="space-y-3 border-t border-border px-3 py-3 text-xs">
              {decomposition.map((bucket, bIdx) => (
                <div key={bIdx} className="rounded-md border border-border p-2">
                  <div className="mb-1.5 font-semibold text-foreground">{bucket.scope_bucket || `Scope #${bIdx + 1}`}</div>
                  {bucket.manpower && bucket.manpower.length > 0 && (
                    <div className="mb-2">
                      <div className="mb-0.5 text-[11px] font-medium text-muted-foreground">Manpower</div>
                      <table className="w-full text-[11px]">
                        <thead className="text-muted-foreground">
                          <tr>
                            <th className="text-left font-normal">Role</th>
                            <th className="text-right font-normal">Headcount</th>
                            <th className="text-left font-normal">Deployment</th>
                            <th className="text-right font-normal">Rate</th>
                            <th className="text-left font-normal">Unit</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bucket.manpower.map((m, i) => (
                            <tr key={i} className="border-t border-border">
                              <td>{m.role || ''}</td>
                              <td className="text-right">{m.headcount ?? ''}</td>
                              <td>{m.deployment || ''}</td>
                              <td className="text-right">
                                {m.rate_low_inr != null && m.rate_high_inr != null
                                  ? `${fmtMoney(m.rate_low_inr)} – ${fmtMoney(m.rate_high_inr)}`
                                  : fmtMoney(m.rate_low_inr ?? m.rate_high_inr)}
                              </td>
                              <td className="text-muted-foreground">{m.rate_unit || ''}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {bucket.resources && bucket.resources.length > 0 && (
                    <div className="mb-2">
                      <div className="mb-0.5 text-[11px] font-medium text-muted-foreground">Resources</div>
                      <table className="w-full text-[11px]">
                        <thead className="text-muted-foreground">
                          <tr>
                            <th className="text-left font-normal">Type</th>
                            <th className="text-left font-normal">Item</th>
                            <th className="text-left font-normal">Qty / cycle</th>
                            <th className="text-left font-normal">Frequency</th>
                            <th className="text-right font-normal">Rate</th>
                            <th className="text-left font-normal">Unit</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bucket.resources.map((r, i) => (
                            <tr key={i} className="border-t border-border">
                              <td>{r.type || ''}</td>
                              <td>{r.item || ''}</td>
                              <td>{String(r.quantity_per_cycle ?? '')}</td>
                              <td>{r.frequency || ''}</td>
                              <td className="text-right">
                                {r.rate_low_inr != null && r.rate_high_inr != null
                                  ? `${fmtMoney(r.rate_low_inr)} – ${fmtMoney(r.rate_high_inr)}`
                                  : fmtMoney(r.rate_low_inr ?? r.rate_high_inr)}
                              </td>
                              <td className="text-muted-foreground">{r.rate_unit || ''}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {bucket.volume_drivers && bucket.volume_drivers.length > 0 && (
                    <div className="text-[11px] text-muted-foreground">
                      <span className="font-medium">Volume drivers:</span> {bucket.volume_drivers.join('; ')}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Editable percentages */}
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <label className="flex items-center gap-1.5">
          <span className="font-medium text-foreground">Overhead %</span>
          <input
            type="number" step="0.1" min={0} max={100}
            value={overhead}
            onChange={(e) => { setOverhead(parseFloat(e.target.value) || 0); setDirty(true); }}
            className="w-16 rounded border border-border px-2 py-1 text-right"
          />
        </label>
        <label className="flex items-center gap-1.5">
          <span className="font-medium text-foreground">Default Margin %</span>
          <input
            type="number" step="0.1" min={0} max={100}
            value={margin}
            onChange={(e) => { setMargin(parseFloat(e.target.value) || 0); setDirty(true); }}
            className="w-16 rounded border border-border px-2 py-1 text-right"
          />
        </label>
        <label className="flex items-center gap-1.5">
          <span className="font-medium text-foreground">GST %</span>
          <input
            type="number" step="0.1" min={0} max={100}
            value={gst}
            onChange={(e) => { setGst(parseFloat(e.target.value) || 0); setDirty(true); }}
            className="w-16 rounded border border-border px-2 py-1 text-right"
          />
        </label>
        <button
          onClick={handleResetPercents}
          className="ml-auto inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
          title="Reset to saved values"
        >
          <RotateCcw size={12} /> reset
        </button>
      </div>

      {/* Lines table — column set adapts to mode (margin / range / legacy) */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="min-w-full text-xs">
          <thead className="bg-muted/40 text-foreground">
            <tr>
              <th className="w-10 px-2 py-2 text-left font-semibold">#</th>
              <th className="px-2 py-2 text-left font-semibold">Description</th>
              {!showMarginColumns && (
                <th className="w-24 px-2 py-2 text-left font-semibold">Category</th>
              )}
              <th className="w-16 px-2 py-2 text-right font-semibold">Qty</th>
              <th className="w-14 px-2 py-2 text-left font-semibold">{showMarginColumns ? 'UoM' : 'Unit'}</th>
              {showMarginColumns && (
                <>
                  <th className="w-22 px-2 py-2 text-right font-semibold" title="Rate stated in the tender BOQ">Tender Rate</th>
                  <th className="w-24 px-2 py-2 text-right font-semibold" title="tender_rate × quantity">Tender Amt</th>
                </>
              )}
              {showRangeColumns && !showMarginColumns && (
                <th className="w-20 px-2 py-2 text-right font-semibold" title="Optimistic credible rate">Rate Low</th>
              )}
              <th className="w-22 px-2 py-2 text-right font-semibold">
                {showMarginColumns ? 'Est. Cost' : (showRangeColumns ? 'Rate Exp.' : 'Rate')}
              </th>
              {showRangeColumns && !showMarginColumns && (
                <th className="w-20 px-2 py-2 text-right font-semibold" title="Conservative credible rate">Rate High</th>
              )}
              <th className="w-24 px-2 py-2 text-right font-semibold">
                {showMarginColumns ? 'Est. Total' : (showRangeColumns ? 'Amount Exp.' : 'Amount')}
              </th>
              {showMarginColumns && (
                <>
                  <th className="w-22 px-2 py-2 text-right font-semibold" title="tender_amount − estimated_cost (positive = gross margin captured)">Margin (₹)</th>
                  <th className="w-16 px-2 py-2 text-right font-semibold">Margin %</th>
                </>
              )}
              {!showMarginColumns && (
                <th className="w-20 px-2 py-2 text-right font-semibold" title="Leave blank to use the default Margin % above. Type a number to override for this line only.">Margin % <span className="text-muted-foreground font-normal">(override)</span></th>
              )}
              <th className="w-28 px-2 py-2 text-left font-semibold">Source</th>
              <th className="w-10 px-2 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {(showMarginColumns ? lineGroups.flatMap((g) => [
              { schedule: g.schedule, rows: g.rows },
            ]) : [{ schedule: '', rows: lines.map((line, idx) => ({ idx, line })) }]).map((group, groupIdx) => (
              <>
                {showMarginColumns && lineGroups.length > 1 && (
                  <tr
                    key={`grp-${groupIdx}`}
                    className="cursor-pointer select-none bg-accent/5 hover:bg-accent/15/50"
                    onClick={() => toggleGroup(group.schedule)}
                  >
                    <td colSpan={20} className="px-2 py-1.5 text-xs font-semibold text-accent">
                      <span className="inline-flex items-center gap-1">
                        {collapsedGroups.has(group.schedule)
                          ? <ChevronRight size={12} />
                          : <ChevronDown size={12} />}
                        {group.schedule === '—' ? 'Unscheduled lines' : group.schedule}
                        <span className="font-normal text-accent/70">({group.rows.length})</span>
                      </span>
                    </td>
                  </tr>
                )}
                {!(showMarginColumns && lineGroups.length > 1 && collapsedGroups.has(group.schedule)) && group.rows.map(({ idx, line: ln }) => {
              const expectedAmt = ln.amount ?? computeAmountAt(ln, 'expected');
              const lowAmt = ln.amount_low ?? computeAmountAt(ln, 'low');
              const highAmt = ln.amount_high ?? computeAmountAt(ln, 'high');
              const isTaxLine = !!ln.is_tax_line;
              const rowBg = isTaxLine
                ? 'bg-orange-50 dark:bg-orange-500/15'
                : ln.needs_input
                ? 'bg-amber-50/40'
                : '';
              // Build a small NIT-source caption (item code · schedule · bidding unit)
              // when the row is bound to a captured BOQItem. This is the "where did
              // this row come from" anchor that proves RULE 5 1:1 mirror.
              const isComponent = ln.parent_boq_item_id != null;
              const nitTag = [
                ln.schedule_name ? `Sch ${ln.schedule_name}` : null,
                ln.item_code ? `Code ${ln.item_code}` : null,
                ln.bidding_unit ? ln.bidding_unit : null,
                isComponent ? `component · qty per set · not added to totals` : null,
                !isComponent && ln.rate_source === 'component_buildup' ? 'rate = sum of its annexure components' : null,
              ]
                .filter(Boolean)
                .join(' · ');
              return (
                <tr
                  key={ln.id ?? `new-${idx}`}
                  className={`border-t border-border ${rowBg}`}
                  title={ln.boq_item_id ? `Linked to NIT BOQItem #${ln.boq_item_id}` : undefined}
                >
                  <td className="px-2 py-1.5 text-center text-muted-foreground">{ln.sr_no}</td>
                  <td className="px-2 py-1.5">
                    <div className="flex items-start gap-1.5">
                      <input
                        type="text"
                        value={ln.description}
                        onChange={(e) => updateLine(idx, { description: e.target.value })}
                        readOnly={isTaxLine}
                        className={`w-full rounded border border-transparent bg-transparent px-1 py-0.5 hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none ${
                          isTaxLine ? 'text-orange-900' : ''
                        }`}
                      />
                      {isTaxLine && (
                        <span className="mt-0.5 inline-block rounded bg-orange-200 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-orange-900">
                          Tax
                        </span>
                      )}
                    </div>
                    {nitTag && (
                      <div className="mt-0.5 text-[10px] font-mono text-accent">{nitTag}</div>
                    )}
                    {(ln.cost_buildup_note || ln.source_ref) && (
                      <div className="mt-0.5 text-[10px] italic text-muted-foreground" title={ln.cost_buildup_note || ln.source_ref || undefined}>
                        {((ln.cost_buildup_note || ln.source_ref) as string).length > 80
                          ? ((ln.cost_buildup_note || ln.source_ref) as string).slice(0, 80) + '…'
                          : (ln.cost_buildup_note || ln.source_ref)}
                      </div>
                    )}
                  </td>
                  {!showMarginColumns && (
                    <td className="px-2 py-1.5">
                      <input
                        type="text"
                        value={ln.category ?? ''}
                        onChange={(e) => updateLine(idx, { category: e.target.value || null })}
                        className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                      />
                    </td>
                  )}
                  <td className="px-2 py-1.5 text-right">
                    <input
                      type="number"
                      value={ln.quantity ?? ''}
                      step="0.01"
                      onChange={(e) => updateLine(idx, { quantity: e.target.value === '' ? null : parseFloat(e.target.value) })}
                      className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-right hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <input
                      type="text"
                      value={ln.unit ?? ''}
                      onChange={(e) => updateLine(idx, { unit: e.target.value || null })}
                      className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                    />
                  </td>
                  {showMarginColumns && (
                    <>
                      <td className="px-2 py-1.5 text-right">
                        {ln.needs_input ? <span className="text-muted-foreground/50">—</span> : (
                          <input
                            type="number"
                            value={ln.tender_rate ?? ''}
                            step="0.01"
                            placeholder="—"
                            onChange={(e) => updateLine(idx, { tender_rate: e.target.value === '' ? null : parseFloat(e.target.value) })}
                            className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-right text-foreground hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                          />
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right text-muted-foreground">
                        {fmtMoney(ln.tender_amount)}
                      </td>
                    </>
                  )}
                  {showRangeColumns && !showMarginColumns && (
                    <td className="px-2 py-1.5 text-right">
                      {ln.needs_input ? <span className="text-muted-foreground/50">—</span> : (
                        <input
                          type="number"
                          value={ln.rate_low ?? ''}
                          step="0.01"
                          placeholder="—"
                          onChange={(e) => updateLine(idx, { rate_low: e.target.value === '' ? null : parseFloat(e.target.value) })}
                          className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-right text-muted-foreground hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                        />
                      )}
                    </td>
                  )}
                  <td className="px-2 py-1.5 text-right">
                    {ln.needs_input ? (
                      <span className="text-[10px] font-semibold text-amber-700 dark:text-amber-400">[NEEDS RATE]</span>
                    ) : (
                      <input
                        type="number"
                        value={ln.rate ?? ''}
                        step="0.01"
                        placeholder="—"
                        onChange={(e) => updateLine(idx, { rate: e.target.value === '' ? null : parseFloat(e.target.value) })}
                        className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-right font-medium hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                      />
                    )}
                  </td>
                  {showRangeColumns && !showMarginColumns && (
                    <td className="px-2 py-1.5 text-right">
                      {ln.needs_input ? <span className="text-muted-foreground/50">—</span> : (
                        <input
                          type="number"
                          value={ln.rate_high ?? ''}
                          step="0.01"
                          placeholder="—"
                          onChange={(e) => updateLine(idx, { rate_high: e.target.value === '' ? null : parseFloat(e.target.value) })}
                          className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-right text-muted-foreground hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                        />
                      )}
                    </td>
                  )}
                  <td className="px-2 py-1.5 text-right font-medium text-foreground">
                    {fmtMoney(expectedAmt)}
                    {showRangeColumns && !showMarginColumns && lowAmt != null && highAmt != null && (lowAmt !== expectedAmt || highAmt !== expectedAmt) && (
                      <div className="text-[9px] font-normal text-muted-foreground">
                        {fmtMoney(lowAmt)} – {fmtMoney(highAmt)}
                      </div>
                    )}
                  </td>
                  {showMarginColumns && (
                    <>
                      <td className={`px-2 py-1.5 text-right font-medium ${ln.margin_amount != null && ln.margin_amount < 0 ? 'text-red-600 dark:text-red-400' : 'text-foreground'}`}>
                        {fmtMoney(ln.margin_amount)}
                      </td>
                      <td className={`px-2 py-1.5 text-right ${ln.margin_pct != null && ln.margin_pct < 0 ? 'text-red-600 dark:text-red-400 font-semibold' : 'text-muted-foreground'}`}>
                        {ln.margin_pct != null ? `${Number(ln.margin_pct).toFixed(1)}%` : '—'}
                      </td>
                    </>
                  )}
                  {!showMarginColumns && (
                    <td className="px-2 py-1.5 text-right">
                      {ln.needs_input ? <span className="text-muted-foreground/50">—</span> : (
                        <input
                          type="number"
                          value={ln.profit_pct ?? ''}
                          step="0.1"
                          placeholder={`${margin}`}
                          title={ln.profit_pct == null ? `Using default ${margin}% — leave blank to keep, type a number to override` : `Override: this line uses ${ln.profit_pct}%`}
                          onChange={(e) => updateLine(idx, { profit_pct: e.target.value === '' ? null : parseFloat(e.target.value) })}
                          className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 text-right text-muted-foreground hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                        />
                      )}
                    </td>
                  )}
                  <td className="px-2 py-1.5">
                    <select
                      value={ln.rate_source ?? ''}
                      onChange={(e) => {
                        const newSource = e.target.value || null;
                        const isNeedsInput = newSource === 'needs_user_input';
                        updateLine(idx, { rate_source: newSource, needs_input: isNeedsInput });
                      }}
                      className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 hover:border-border focus:border-accent/40 focus:bg-card focus:outline-none"
                    >
                      {SOURCE_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </td>
                  <td className="px-2 py-1.5 text-center">
                    <button
                      onClick={() => removeLine(idx)}
                      className="text-muted-foreground hover:text-red-600 dark:text-red-400"
                      title="Remove row"
                    >
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              );
                })}
              </>
            ))}
          </tbody>
        </table>
      </div>

      {/* Phase 3b — Cost Assumptions library (matches reference Sheet 2) */}
      {costAssumptions.length > 0 && (
        <div className="rounded-lg border border-border p-3">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-foreground">
            Cost Assumptions (Rate-Card Library)
          </div>
          <div className="space-y-3">
            {Object.entries(
              costAssumptions.reduce<Record<string, typeof costAssumptions>>((acc, entry) => {
                const sec = (entry.section || 'Other').trim() || 'Other';
                (acc[sec] ||= []).push(entry);
                return acc;
              }, {}),
            ).map(([sec, entries]) => (
              <div key={sec}>
                <div className="mb-1 text-[11px] font-semibold text-foreground">{sec}</div>
                <table className="w-full text-[11px]">
                  <thead className="text-muted-foreground">
                    <tr>
                      <th className="text-left font-normal">Item</th>
                      <th className="w-20 text-right font-normal">Rate (₹)</th>
                      <th className="w-14 text-left font-normal">UoM</th>
                      <th className="text-left font-normal">Source / remark</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry, i) => (
                      <tr key={i} className="border-t border-border">
                        <td className="py-0.5">{entry.item}</td>
                        <td className="py-0.5 text-right">{fmtMoney(entry.rate_inr)}</td>
                        <td className="py-0.5 text-muted-foreground">{entry.uom}</td>
                        <td className="py-0.5 text-muted-foreground">{entry.source_ref}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>
      )}

      <button
        onClick={addLine}
        className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:border-accent/40 hover:bg-muted/40"
      >
        <Plus size={13} /> Add line
      </button>

      {/* Totals — Low / Expected / High table when range data is available,
          legacy single-column totals otherwise. */}
      <div className="rounded-lg border border-border bg-muted/40 p-3">
        {showRangeColumns ? (
          <table className="ml-auto min-w-[460px] text-xs">
            <thead>
              <tr className="text-muted-foreground">
                <th className="text-left font-medium">Component</th>
                <th className="pl-4 text-right font-medium">Low</th>
                <th className="pl-4 text-right font-medium">Expected</th>
                <th className="pl-4 text-right font-medium">High</th>
              </tr>
            </thead>
            <tbody>
              <tr><td className="py-0.5 text-muted-foreground">Subtotal</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.low.subtotal)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.subtotal)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.high.subtotal)}</td></tr>
              <tr><td className="py-0.5 text-muted-foreground">Overhead ({overhead}%)</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.low.overheadAmt)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.overheadAmt)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.high.overheadAmt)}</td></tr>
              <tr><td className="py-0.5 text-muted-foreground">Margin</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.low.marginAmt)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.marginAmt)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.high.marginAmt)}</td></tr>
              <tr><td className="py-0.5 text-muted-foreground">GST ({gst}%)</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.low.gstAmt)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.gstAmt)}</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.high.gstAmt)}</td></tr>
              <tr className="border-t border-border font-bold">
                <td className="py-1 text-foreground">Grand Total</td>
                <td className="py-1 pl-4 text-right text-foreground">{fmtMoney(totals.low.grand)}</td>
                <td className="py-1 pl-4 text-right text-foreground">{fmtMoney(totals.expected.grand)}</td>
                <td className="py-1 pl-4 text-right text-foreground">{fmtMoney(totals.high.grand)}</td>
              </tr>
            </tbody>
          </table>
        ) : (
          <table className="ml-auto min-w-[280px] text-xs">
            <tbody>
              <tr><td className="py-0.5 text-muted-foreground">Subtotal</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.subtotal)}</td></tr>
              <tr><td className="py-0.5 text-muted-foreground">Overhead ({overhead}%)</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.overheadAmt)}</td></tr>
              <tr><td className="py-0.5 text-muted-foreground">Margin ({margin}%)</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.marginAmt)}</td></tr>
              <tr><td className="py-0.5 text-muted-foreground">GST ({gst}%)</td><td className="py-0.5 pl-4 text-right">{fmtMoney(totals.expected.gstAmt)}</td></tr>
              <tr className="border-t border-border font-bold"><td className="py-1 text-foreground">Grand Total</td><td className="py-1 pl-4 text-right text-foreground">{fmtMoney(totals.expected.grand)}</td></tr>
            </tbody>
          </table>
        )}
      </div>

      {/* Recommendations / questions from the agent */}
      {breakdown.recommendations.length > 0 && (
        <div className="rounded-lg border border-accent/20 bg-accent/10 p-3 text-xs">
          <div className="mb-1 font-semibold text-accent">Agent recommendations</div>
          <ul className="list-disc space-y-0.5 pl-5 text-accent">
            {breakdown.recommendations.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/15 px-3 py-2 text-xs text-red-700 dark:text-red-400">{error}</div>
      )}
    </div>
  );
}
