import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Filter as FilterIcon, X } from 'lucide-react';
import type {
  SessionDateRange,
  SessionFilter,
  SessionModeFilter,
  SessionSortKind,
  SessionStatusFilter,
} from './applySessionFilters';

interface SessionFilterBarProps {
  filter: SessionFilter;
  onFilterChange: (next: SessionFilter) => void;
  sort: SessionSortKind;
  onSortChange: (next: SessionSortKind) => void;
  activeFilterCount: number;
  onClearFilters: () => void;
  /** When true, render a compact variant suitable for the narrow sidebar rail. */
  compact?: boolean;
}

const STATUS_OPTIONS: Array<{ value: SessionStatusFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'draft', label: 'Draft' },
  { value: 'submitted', label: 'Submitted' },
  { value: 'under_review', label: 'Review' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'revision_requested', label: 'Needs revision' },
];

const MODE_OPTIONS: Array<{ value: SessionModeFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'tender_linked', label: 'Tender' },
  { value: 'standalone', label: 'Ad-hoc' },
];

const DATE_OPTIONS: Array<{ value: SessionDateRange; label: string }> = [
  { value: 'all', label: 'All time' },
  { value: 'last_7d', label: 'Last 7d' },
  { value: 'last_30d', label: 'Last 30d' },
  { value: 'custom', label: 'Custom' },
];

const SORT_OPTIONS: Array<{ value: SessionSortKind; label: string }> = [
  { value: 'recency', label: 'Recency' },
  { value: 'created', label: 'Created' },
  { value: 'name', label: 'Name' },
  { value: 'status', label: 'Status' },
];

/**
 * Filter chips + sort dropdown for the Command Center session list.
 *
 * Two layouts:
 *   - Full (default): inline horizontal pills, full set of chips visible.
 *   - Compact: a single "Filter" button opens a small popover with the same
 *     controls, plus a small sort selector. Suitable for narrow surfaces.
 */
export default function SessionFilterBar({
  filter,
  onFilterChange,
  sort,
  onSortChange,
  activeFilterCount,
  onClearFilters,
  compact,
}: SessionFilterBarProps) {
  if (compact) {
    return (
      <CompactBar
        filter={filter}
        onFilterChange={onFilterChange}
        sort={sort}
        onSortChange={onSortChange}
        activeFilterCount={activeFilterCount}
        onClearFilters={onClearFilters}
      />
    );
  }
  return (
    <FullBar
      filter={filter}
      onFilterChange={onFilterChange}
      sort={sort}
      onSortChange={onSortChange}
      activeFilterCount={activeFilterCount}
      onClearFilters={onClearFilters}
    />
  );
}

// ── Full bar ────────────────────────────────────────────────────────────────

function FullBar(props: Omit<SessionFilterBarProps, 'compact'>) {
  const { filter, onFilterChange, sort, onSortChange, activeFilterCount, onClearFilters } = props;
  return (
    <div className="flex flex-wrap items-center gap-2 mb-4">
      <ChipGroup
        label="Status"
        value={filter.status}
        options={STATUS_OPTIONS}
        onChange={(v) => onFilterChange({ ...filter, status: v as SessionStatusFilter })}
      />
      <ChipGroup
        label="Mode"
        value={filter.mode}
        options={MODE_OPTIONS}
        onChange={(v) => onFilterChange({ ...filter, mode: v as SessionModeFilter })}
      />
      <DateChipGroup filter={filter} onFilterChange={onFilterChange} />

      {activeFilterCount > 0 && (
        <>
          <span className="text-xs font-medium text-muted-foreground px-1.5">
            {activeFilterCount} filter{activeFilterCount === 1 ? '' : 's'}
          </span>
          <button
            onClick={onClearFilters}
            className="text-xs text-accent hover:text-accent/80 hover:underline"
          >
            Clear all
          </button>
        </>
      )}

      <div className="ml-auto">
        <SortSelect value={sort} onChange={onSortChange} />
      </div>
    </div>
  );
}

// ── Compact bar (sidebar) ───────────────────────────────────────────────────

function CompactBar(props: Omit<SessionFilterBarProps, 'compact'>) {
  const { filter, onFilterChange, sort, onSortChange, activeFilterCount, onClearFilters } = props;
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Click outside to close
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  return (
    <div className="px-2 pt-1 pb-2 border-b border-border">
      <div className="flex items-center gap-1.5">
        <div className="relative flex-1" ref={popoverRef}>
          <button
            onClick={() => setOpen((v) => !v)}
            className={`w-full flex items-center justify-between gap-1.5 px-2 py-1 rounded-md text-xs border ${
              activeFilterCount > 0
                ? 'bg-accent/10 border-accent/20 text-accent'
                : 'bg-card border-border text-muted-foreground hover:bg-muted/40'
            }`}
            title="Filter sessions"
            aria-label="Filter sessions"
            aria-expanded={open}
          >
            <span className="inline-flex items-center gap-1.5">
              <FilterIcon size={12} />
              <span>Filter</span>
              {activeFilterCount > 0 && (
                <span className="px-1 rounded bg-accent/15 text-accent text-[10px]">
                  {activeFilterCount}
                </span>
              )}
            </span>
            <ChevronDown size={12} />
          </button>

          {open && (
            <div className="absolute left-0 right-0 top-full mt-1 z-30 bg-card rounded-lg border border-border shadow-xl p-3 space-y-3">
              <ChipGroup
                label="Status"
                value={filter.status}
                options={STATUS_OPTIONS}
                onChange={(v) => onFilterChange({ ...filter, status: v as SessionStatusFilter })}
                stack
              />
              <ChipGroup
                label="Mode"
                value={filter.mode}
                options={MODE_OPTIONS}
                onChange={(v) => onFilterChange({ ...filter, mode: v as SessionModeFilter })}
                stack
              />
              <DateChipGroup filter={filter} onFilterChange={onFilterChange} stack />
              {activeFilterCount > 0 && (
                <button
                  onClick={() => {
                    onClearFilters();
                    setOpen(false);
                  }}
                  className="text-xs text-accent hover:text-accent/80 hover:underline inline-flex items-center gap-1"
                >
                  <X size={10} /> Clear all
                </button>
              )}
            </div>
          )}
        </div>
        <SortSelect value={sort} onChange={onSortChange} compact />
      </div>
    </div>
  );
}

// ── Shared sub-components ───────────────────────────────────────────────────

function ChipGroup<T extends string>({
  label,
  value,
  options,
  onChange,
  stack,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (next: T) => void;
  stack?: boolean;
}) {
  return (
    <div className={stack ? 'space-y-1.5' : 'inline-flex items-center gap-1.5'}>
      <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <div className="inline-flex flex-wrap gap-1">
        {options.map((opt) => {
          const active = opt.value === value;
          return (
            <button
              key={opt.value}
              onClick={() => onChange(opt.value)}
              aria-pressed={active}
              className={`px-2 py-0.5 rounded-full text-[11px] font-medium border transition-colors ${
                active
                  ? 'bg-accent text-accent-foreground border-blue-600'
                  : 'bg-card text-muted-foreground border-border hover:bg-muted/40'
              }`}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DateChipGroup({
  filter,
  onFilterChange,
  stack,
}: {
  filter: SessionFilter;
  onFilterChange: (next: SessionFilter) => void;
  stack?: boolean;
}) {
  return (
    <div className={stack ? 'space-y-1.5' : 'inline-flex items-center gap-1.5'}>
      <ChipGroup
        label="Date"
        value={filter.dateRange}
        options={DATE_OPTIONS}
        onChange={(v) => onFilterChange({ ...filter, dateRange: v as SessionDateRange })}
        stack={stack}
      />
      {filter.dateRange === 'custom' && (
        <div className="inline-flex items-center gap-1.5 ml-2">
          <input
            type="date"
            value={filter.customStart ?? ''}
            onChange={(e) =>
              onFilterChange({ ...filter, customStart: e.target.value || undefined })
            }
            className="px-1.5 py-0.5 rounded border border-border text-[11px]"
            aria-label="Custom start date"
          />
          <span className="text-[11px] text-muted-foreground">→</span>
          <input
            type="date"
            value={filter.customEnd ?? ''}
            onChange={(e) =>
              onFilterChange({ ...filter, customEnd: e.target.value || undefined })
            }
            className="px-1.5 py-0.5 rounded border border-border text-[11px]"
            aria-label="Custom end date"
          />
        </div>
      )}
    </div>
  );
}

function SortSelect({
  value,
  onChange,
  compact,
}: {
  value: SessionSortKind;
  onChange: (next: SessionSortKind) => void;
  compact?: boolean;
}) {
  return (
    <label className="inline-flex items-center gap-1.5">
      {!compact && (
        <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wide">
          Sort
        </span>
      )}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as SessionSortKind)}
        className={`bg-card border border-border rounded text-foreground hover:bg-muted/40 focus:outline-none focus:ring-1 focus:ring-ring ${
          compact ? 'text-[11px] px-1.5 py-0.5' : 'text-xs px-2 py-1'
        }`}
        aria-label="Sort sessions"
      >
        {SORT_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}
