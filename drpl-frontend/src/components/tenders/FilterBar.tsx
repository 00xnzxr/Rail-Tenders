import { useMemo, useRef, useState } from 'react';
import { Search, ChevronDown, SlidersHorizontal, X } from 'lucide-react';
import { addDays, format } from 'date-fns';
import { cn } from '@/lib/utils';

/**
 * The Tenders filter bar. Replaces the old collapsible "Refine" wall: the
 * four filters people actually reach for (Score, Value, Closing, Portal) live
 * inline as dropdown chips; everything rarer folds into "More filters".
 * Active filters show as removable tags. Changes apply immediately — the whole
 * active set is emitted on every change so the parent can merge it over the
 * view's base filters.
 */

export type FilterDelta = {
  search?: string;
  department?: string;
  location?: string;
  portal?: string;
  status?: string;
  workflow_status?: string;
  bid_type?: string;
  eligibility_status?: string;
  sort_by?: string;
  score_min?: number;
  score_max?: number;
  value_min?: number;
  value_max?: number;
  emd_min?: number;
  emd_max?: number;
  closing_after?: string;
  closing_before?: string;
};

interface Props {
  onApply: (delta: FilterDelta) => void;
}

type ScorePreset = { key: string; label: string; min?: number; max?: number };
const SCORE_PRESETS: ScorePreset[] = [
  { key: 'any', label: 'Any score' },
  { key: '90', label: '≥ 90%', min: 0.9 },
  { key: '70', label: '70 – 90%', min: 0.7, max: 0.9 },
  { key: '40', label: '40 – 70%', min: 0.4, max: 0.7 },
  { key: 'lo', label: '< 40%', max: 0.4 },
];

type ValuePreset = { key: string; label: string; min?: number; max?: number };
const VALUE_PRESETS: ValuePreset[] = [
  { key: 'any', label: 'Any value' },
  { key: 's', label: '< ₹50 L', max: 5_000_000 },
  { key: 'm', label: '₹50 L – 1 Cr', min: 5_000_000, max: 10_000_000 },
  { key: 'l', label: '≥ ₹1 Cr', min: 10_000_000 },
];

type ClosePreset = { key: string; label: string; days?: number };
const CLOSE_PRESETS: ClosePreset[] = [
  { key: 'any', label: 'Any time' },
  { key: '7', label: 'This week', days: 7 },
  { key: '30', label: 'Next 30 days', days: 30 },
  { key: '90', label: 'Next 90 days', days: 90 },
];

const PORTALS = [
  { key: '', label: 'All portals' },
  { key: 'ireps', label: 'IREPS' },
  { key: 'gem', label: 'GeM' },
  { key: 'tendertiger', label: 'TenderTiger' },
  { key: 'bidassist', label: 'BidAssist' },
];

const SELECT =
  'w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring';
const FIELD_LABEL = 'block text-xs font-medium text-muted-foreground mb-1';

// ---- inline dropdown chip ------------------------------------------------
function Dropdown({
  label, value, active, children,
}: {
  label: string; value: string; active: boolean; children: (close: () => void) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          'inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-[13px] font-medium transition-colors',
          active
            ? 'border-accent bg-accent/10 text-accent'
            : 'border-border text-foreground hover:border-foreground/25',
        )}
      >
        <span>{label}</span>
        <span className={cn('font-normal', active ? 'text-accent' : 'text-muted-foreground')}>{value}</span>
        <ChevronDown size={13} className={cn('transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <>
          <button type="button" aria-hidden className="fixed inset-0 z-30 cursor-default" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-40 mt-1.5 min-w-[180px] rounded-xl border border-border bg-card p-1.5 shadow-lg">
            {children(() => setOpen(false))}
          </div>
        </>
      )}
    </div>
  );
}

function MenuItem({ selected, onClick, children }: { selected: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-[13px] transition-colors',
        selected ? 'bg-accent/10 font-semibold text-accent' : 'text-foreground hover:bg-muted',
      )}
    >
      {children}
    </button>
  );
}

export default function FilterBar({ onApply }: Props) {
  const [f, setF] = useState<FilterDelta>({});
  const [scoreKey, setScoreKey] = useState('any');
  const [valueKey, setValueKey] = useState('any');
  const [closeKey, setCloseKey] = useState('any');
  const [search, setSearch] = useState('');
  const [moreOpen, setMoreOpen] = useState(false);
  const searchTimer = useRef<number | undefined>(undefined);

  const emit = (next: FilterDelta) => {
    setF(next);
    onApply(next);
  };

  const patch = (delta: Partial<FilterDelta>) => emit({ ...f, ...delta });

  const setScore = (p: ScorePreset) => { setScoreKey(p.key); patch({ score_min: p.min, score_max: p.max }); };
  const setValue = (p: ValuePreset) => { setValueKey(p.key); patch({ value_min: p.min, value_max: p.max }); };
  const setClose = (p: ClosePreset) => {
    setCloseKey(p.key);
    if (p.days == null) patch({ closing_after: undefined, closing_before: undefined });
    else patch({ closing_after: new Date().toISOString(), closing_before: format(addDays(new Date(), p.days), 'yyyy-MM-dd') });
  };

  const onSearch = (v: string) => {
    setSearch(v);
    window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(() => patch({ search: v.trim() || undefined }), 350);
  };

  // Active-filter tags (sort_by excluded — it's a preference, not a filter).
  const tags = useMemo(() => {
    const t: { key: string; label: string; clear: () => void }[] = [];
    if (scoreKey !== 'any') t.push({ key: 'score', label: `Score ${SCORE_PRESETS.find((p) => p.key === scoreKey)!.label}`, clear: () => setScore(SCORE_PRESETS[0]) });
    if (valueKey !== 'any') t.push({ key: 'value', label: VALUE_PRESETS.find((p) => p.key === valueKey)!.label, clear: () => setValue(VALUE_PRESETS[0]) });
    if (closeKey !== 'any') t.push({ key: 'close', label: `Closing ${CLOSE_PRESETS.find((p) => p.key === closeKey)!.label.toLowerCase()}`, clear: () => setClose(CLOSE_PRESETS[0]) });
    if (f.portal) t.push({ key: 'portal', label: PORTALS.find((p) => p.key === f.portal)?.label ?? f.portal, clear: () => patch({ portal: undefined }) });
    if (f.status) t.push({ key: 'status', label: `Status: ${f.status}`, clear: () => patch({ status: undefined }) });
    if (f.workflow_status) t.push({ key: 'wf', label: `Stage: ${f.workflow_status}`, clear: () => patch({ workflow_status: undefined }) });
    if (f.bid_type) t.push({ key: 'bt', label: f.bid_type, clear: () => patch({ bid_type: undefined }) });
    if (f.eligibility_status) t.push({ key: 'el', label: `Eligibility: ${f.eligibility_status}`, clear: () => patch({ eligibility_status: undefined }) });
    if (f.location) t.push({ key: 'loc', label: `Location: ${f.location}`, clear: () => patch({ location: undefined }) });
    if (f.emd_min != null || f.emd_max != null) t.push({ key: 'emd', label: 'EMD range', clear: () => patch({ emd_min: undefined, emd_max: undefined }) });
    if (f.search) t.push({ key: 'search', label: `“${f.search}”`, clear: () => { setSearch(''); patch({ search: undefined }); } });
    return t;
  }, [scoreKey, valueKey, closeKey, f]);

  const resetAll = () => {
    setScoreKey('any'); setValueKey('any'); setCloseKey('any'); setSearch('');
    emit({});
  };

  const portalLabel = PORTALS.find((p) => p.key === (f.portal ?? ''))!.label;

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-border bg-card p-2 shadow-card">
        {/* search */}
        <label className="flex min-w-[220px] basis-full items-center gap-2 rounded-xl border border-border bg-background px-3.5 py-2.5 md:basis-auto md:flex-1">
          <Search size={15} className="shrink-0 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search tender titles…"
            aria-label="Search tender titles"
            className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
        </label>

        {/* score */}
        <Dropdown label="Score" value={SCORE_PRESETS.find((p) => p.key === scoreKey)!.label} active={scoreKey !== 'any'}>
          {(close) => SCORE_PRESETS.map((p) => (
            <MenuItem key={p.key} selected={p.key === scoreKey} onClick={() => { setScore(p); close(); }}>{p.label}</MenuItem>
          ))}
        </Dropdown>

        {/* value */}
        <Dropdown label="Value" value={VALUE_PRESETS.find((p) => p.key === valueKey)!.label} active={valueKey !== 'any'}>
          {(close) => VALUE_PRESETS.map((p) => (
            <MenuItem key={p.key} selected={p.key === valueKey} onClick={() => { setValue(p); close(); }}>{p.label}</MenuItem>
          ))}
        </Dropdown>

        {/* closing */}
        <Dropdown label="Closing" value={CLOSE_PRESETS.find((p) => p.key === closeKey)!.label} active={closeKey !== 'any'}>
          {(close) => CLOSE_PRESETS.map((p) => (
            <MenuItem key={p.key} selected={p.key === closeKey} onClick={() => { setClose(p); close(); }}>{p.label}</MenuItem>
          ))}
        </Dropdown>

        {/* portal */}
        <Dropdown label="Portal" value={portalLabel} active={!!f.portal}>
          {(close) => PORTALS.map((p) => (
            <MenuItem key={p.key} selected={(f.portal ?? '') === p.key} onClick={() => { patch({ portal: p.key || undefined }); close(); }}>{p.label}</MenuItem>
          ))}
        </Dropdown>

        <button
          type="button"
          onClick={() => setMoreOpen((v) => !v)}
          aria-expanded={moreOpen}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-[13px] font-medium transition-colors',
            moreOpen ? 'border-foreground/30 text-foreground' : 'border-border text-muted-foreground hover:text-foreground',
          )}
        >
          <SlidersHorizontal size={14} />
          More filters
        </button>
      </div>

      {/* more-filters drawer */}
      {moreOpen && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-xl border border-border bg-card p-4 sm:grid-cols-3 lg:grid-cols-4">
          <div>
            <label className={FIELD_LABEL}>Status</label>
            <select className={SELECT} value={f.status ?? ''} onChange={(e) => patch({ status: e.target.value || undefined })}>
              <option value="">All statuses</option>
              <option value="open">Open</option>
              <option value="closed">Closed</option>
              <option value="awarded">Awarded</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </div>
          <div>
            <label className={FIELD_LABEL}>Workflow stage</label>
            <select className={SELECT} value={f.workflow_status ?? ''} onChange={(e) => patch({ workflow_status: e.target.value || undefined })}>
              <option value="">All stages</option>
              <option value="new">New</option>
              <option value="in_progress">In progress</option>
              <option value="checklist_ready">Checklist ready</option>
              <option value="proposal_draft">Proposal draft</option>
              <option value="proposal_review">Under review</option>
              <option value="approved">Approved</option>
              <option value="submitted">Submitted</option>
            </select>
          </div>
          <div>
            <label className={FIELD_LABEL}>Bid type</label>
            <select className={SELECT} value={f.bid_type ?? ''} onChange={(e) => patch({ bid_type: e.target.value || undefined })}>
              <option value="">All types</option>
              <option value="NCB">NCB</option>
              <option value="GCB">GCB</option>
              <option value="Limited">Limited</option>
              <option value="Single">Single</option>
              <option value="Global">Global</option>
            </select>
          </div>
          <div>
            <label className={FIELD_LABEL}>Eligibility</label>
            <select className={SELECT} value={f.eligibility_status ?? ''} onChange={(e) => patch({ eligibility_status: e.target.value || undefined })}>
              <option value="">All eligibility</option>
              <option value="eligible">Eligible</option>
              <option value="not_eligible">Not eligible</option>
              <option value="unknown">Unknown</option>
            </select>
          </div>
          <div>
            <label className={FIELD_LABEL}>Location</label>
            <input
              type="text"
              value={f.location ?? ''}
              onChange={(e) => patch({ location: e.target.value || undefined })}
              placeholder="City / state…"
              className={SELECT}
            />
          </div>
          <div>
            <label className={FIELD_LABEL}>EMD (₹)</label>
            <div className="flex items-center gap-2">
              <input type="number" placeholder="Min" className={SELECT}
                value={f.emd_min ?? ''} onChange={(e) => patch({ emd_min: e.target.value === '' ? undefined : Number(e.target.value) })} />
              <span className="text-xs text-muted-foreground">–</span>
              <input type="number" placeholder="Max" className={SELECT}
                value={f.emd_max ?? ''} onChange={(e) => patch({ emd_max: e.target.value === '' ? undefined : Number(e.target.value) })} />
            </div>
          </div>
          <div>
            <label className={FIELD_LABEL}>Sort by</label>
            <select className={SELECT} value={f.sort_by ?? 'relevance'} onChange={(e) => patch({ sort_by: e.target.value })}>
              <option value="relevance">Best match</option>
              <option value="closing_date">Closing date</option>
              <option value="submission_deadline">Submission deadline</option>
              <option value="created_at">Newest first</option>
              <option value="priority">Priority</option>
            </select>
          </div>
        </div>
      )}

      {/* active filter tags */}
      {tags.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 pl-0.5">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Filtering</span>
          {tags.map((t) => (
            <span key={t.key} className="inline-flex items-center gap-1.5 rounded-full bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent">
              {t.label}
              <button type="button" onClick={t.clear} aria-label={`Remove ${t.label}`} className="opacity-70 hover:opacity-100">
                <X size={12} />
              </button>
            </span>
          ))}
          <button type="button" onClick={resetAll} className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground">
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}
