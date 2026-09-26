import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MapPin, Building2, Clock, FileText, LayoutGrid, ChevronDown, Flag, Archive, XCircle } from 'lucide-react';
import type { Tender } from '../../types/tender';
import { formatCurrency, formatDate, portalLabel } from '../../lib/formatters';
import { differenceInDays, parseISO } from 'date-fns';

interface TenderCardProps {
  tender: Tender;
  searchQuery?: string;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: number) => void;
  variant?: 'default' | 'discarded';
}

function HighlightedTitle({ title, searchQuery }: { title: string; searchQuery?: string }) {
  const terms = [...new Set((searchQuery ?? '').trim().split(/\s+/).filter(Boolean))];
  if (terms.length === 0) return <>{title}</>;

  const escapedTerms = terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const matcher = new RegExp(`(${escapedTerms.join('|')})`, 'gi');
  const isMatch = new RegExp(`^(${escapedTerms.join('|')})$`, 'i');
  return <>{title.split(matcher).map((part, index) => (
    isMatch.test(part)
      ? <mark key={index} className="rounded bg-amber-200/80 px-0.5 text-inherit dark:bg-amber-400/30">{part}</mark>
      : part
  ))}</>;
}

function scoreClasses(pct: number): string {
  if (pct >= 70) return 'text-emerald-600 dark:text-emerald-400';
  if (pct >= 40) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function DaysToGo({ closingDate }: { closingDate: string }) {
  const days = differenceInDays(parseISO(closingDate), new Date());
  const base = 'inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold whitespace-nowrap';
  if (days < 0) return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-400`}><Clock size={12} />Expired</span>;
  if (days === 0) return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-400`}><Clock size={12} />Today</span>;
  if (days <= 3) return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-400`}><Clock size={12} />{days} days</span>;
  if (days <= 7) return <span className={`${base} bg-amber-50 text-amber-600 dark:bg-amber-500/15 dark:text-amber-400`}><Clock size={12} />{days} days</span>;
  return <span className={`${base} bg-muted text-muted-foreground`}><Clock size={12} />{days} days</span>;
}

export default function TenderCard({ tender, searchQuery, selectable, selected, onToggleSelect, variant = 'default' }: TenderCardProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const isDiscarded = variant === 'discarded';

  const pct = tender.ai_relevance_score != null ? Math.round(tender.ai_relevance_score * 100) : null;
  // source_portal only meaningful when it differs from the aggregator portal.
  const showOrigin = !!tender.source_portal && tender.source_portal !== tender.portal;
  // Scrapers alias category to the organisation (TenderTiger) or department (GeM),
  // so only show the category badge when it adds information beyond the org line.
  const showCategory = !!tender.category
    && tender.category.trim().toLowerCase() !== (tender.organisation || '').trim().toLowerCase()
    && tender.category.trim().toLowerCase() !== (tender.department || '').trim().toLowerCase();

  return (
    <article className={`grid grid-cols-[64px_minmax(0,1fr)] bg-card border rounded-2xl overflow-hidden transition-all duration-200 sm:grid-cols-[72px_1fr_auto] ${open ? 'border-accent/60 shadow-card-hover' : 'border-border hover:-translate-y-px hover:border-accent/40 hover:shadow-card-hover'} ${isDiscarded ? 'opacity-[0.97]' : ''}`}>
      {/* Left rail: match % + optional select checkbox */}
      <div className={`flex flex-col items-center justify-center gap-1 p-4 border-r border-border relative ${isDiscarded ? 'bg-rose-500/[0.06]' : 'bg-muted/30'}`}>
        {selectable && (
          <input
            type="checkbox"
            checked={!!selected}
            onChange={() => onToggleSelect?.(tender.id)}
            onClick={(e) => e.stopPropagation()}
            className="absolute top-2 left-2 h-4 w-4 rounded border-input text-accent focus:ring-ring cursor-pointer"
            aria-label={`Select tender ${tender.tender_id}`}
          />
        )}
        {pct != null ? (
          <>
            <span className={`text-lg font-extrabold leading-none ${scoreClasses(pct)}`}>{pct}%</span>
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">Match</span>
          </>
        ) : (
          <span className="text-sm text-muted-foreground">—</span>
        )}
      </div>

      {/* Body */}
      <div className="p-4 min-w-0 cursor-pointer" onClick={() => setOpen(o => !o)}>
        <div className="flex items-center gap-2 flex-wrap mb-1.5 text-xs text-muted-foreground">
          <span className="font-mono font-semibold">{tender.tender_id}</span>
          {tender.location && (<><span className="w-1 h-1 rounded-full bg-muted-foreground/50" /><span className="inline-flex items-center gap-1"><MapPin size={12} />{tender.location}</span></>)}
        </div>
        <h2 className="text-[0.98rem] font-bold tracking-tight leading-snug mb-1.5 whitespace-normal break-words"><HighlightedTitle title={tender.title} searchQuery={searchQuery} /></h2>
        <p className="text-sm text-muted-foreground mb-3 inline-flex items-center gap-1.5">
          <Building2 size={13} className="opacity-80" />{tender.organisation || '—'}{tender.department ? ` · ${tender.department}` : ''}
        </p>
        <div className="flex items-center gap-1.5 flex-wrap">
          {showOrigin ? (
            <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-muted text-muted-foreground border border-border">via {portalLabel(tender.portal)} · {portalLabel(tender.source_portal!)}</span>
          ) : (
            <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-accent/10 text-accent border border-accent/25">{portalLabel(tender.portal)}</span>
          )}
          {tender.bid_type && <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/25">{tender.bid_type}</span>}
          {showCategory && <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-muted text-muted-foreground border border-border">{tender.category}</span>}
          <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/25">{tender.status}</span>
        </div>
        {isDiscarded && tender.ai_summary && (
          <p className="mt-2.5 inline-flex items-start gap-1.5 text-xs text-rose-700 dark:text-rose-400 bg-rose-500/[0.08] border border-rose-500/20 rounded-md px-2.5 py-1.5">
            <XCircle size={13} className="mt-px shrink-0" />
            <span className="line-clamp-2">{tender.ai_summary}</span>
          </p>
        )}
        <div className="flex items-center flex-wrap mt-3">
          <div className="flex flex-col pr-4 mr-4 border-r border-border">
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">Worth</span>
            <span className="text-sm font-bold tabular-nums">{formatCurrency(tender.estimated_value)}</span>
          </div>
          <div className="flex flex-col pr-4 mr-4 border-r border-border">
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">EMD</span>
            <span className="text-sm font-bold tabular-nums">{tender.emd_amount != null ? formatCurrency(tender.emd_amount) : '—'}</span>
          </div>
          <div className="flex flex-col">
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">Due date</span>
            <span className="text-sm font-bold tabular-nums">{formatDate(tender.closing_date)}</span>
          </div>
        </div>
      </div>

      {/* Right: days-to-go + expander */}
      <div className="col-span-2 flex items-center justify-between gap-3 border-t border-border px-4 py-3 sm:col-auto sm:flex-col sm:items-end sm:border-t-0 sm:p-4">
        {tender.closing_date && <DaysToGo closingDate={tender.closing_date} />}
        <button onClick={() => setOpen(o => !o)} className="inline-flex items-center gap-1 text-xs text-muted-foreground font-semibold hover:text-foreground" aria-expanded={open}>
          Details <ChevronDown size={16} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
        </button>
      </div>

      {/* Expanded detail */}
      {open && (
        <div className="col-span-2 border-t border-border bg-muted/20 px-5 py-5 sm:col-span-3">
          <div className="grid grid-cols-1 md:grid-cols-[1.5fr_1fr] gap-6">
            <div>
              <p className="text-[0.62rem] uppercase tracking-wider text-muted-foreground font-extrabold mb-1.5">Scope</p>
              <p className="text-sm text-foreground/90 leading-relaxed"><HighlightedTitle title={tender.title} searchQuery={searchQuery} /></p>
            </div>
            <div>
              {tender.ai_summary && (
                <div className="bg-accent/5 border border-accent/20 rounded-lg px-3 py-3">
                  <p className="text-[0.62rem] uppercase tracking-wider text-accent font-extrabold mb-1.5">DRPL fit assessment</p>
                  <p className="text-sm leading-relaxed">{tender.ai_summary}</p>
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2.5 flex-wrap mt-5 pt-4 border-t border-dashed border-border">
            <button onClick={() => navigate(`/tenders/${tender.id}`)} className="inline-flex items-center gap-1.5 text-sm font-bold px-4 py-2 rounded-lg bg-accent text-accent-foreground hover:bg-accent/90">
              <FileText size={15} />View Details
            </button>
            <button onClick={() => navigate(`/tenders/${tender.id}/command-center`)} className="inline-flex items-center gap-1.5 text-sm font-bold px-4 py-2 rounded-lg border border-border hover:border-accent hover:text-accent">
              <LayoutGrid size={15} />Open Command Center
            </button>
            <div className="flex-1" />
            <button title="Set priority" className="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60"><Flag size={15} /></button>
            <button title="Archive" className="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60"><Archive size={15} /></button>
          </div>
        </div>
      )}
    </article>
  );
}
