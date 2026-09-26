import { NavLink } from 'react-router-dom';
import { cn } from '@/lib/utils';
import { FUNNEL_VIEWS, TENDER_VIEWS, type TenderView } from '@/lib/tenderViews';
import type { TenderViewCounts } from '@/lib/api';

/**
 * The signature element of the Tenders page: the four piles rendered as a
 * funnel. Each tab shows its count and a capacity bar sized against the
 * largest pile, so you can see 1,245 tenders narrow down to the handful worth
 * pursuing. Clicking a tab routes to that view.
 */

const viewHref = (slug: string) => (slug ? `/tenders/view/${slug}` : '/tenders');

// tone → {tab accent bg/border/text when active, bar fill, dot} via the app tokens.
const TONE: Record<TenderView['color'], { text: string; bar: string; dot: string; ring: string }> = {
  emerald: { text: 'text-emerald-600 dark:text-emerald-400', bar: 'bg-emerald-500', dot: 'bg-emerald-500', ring: 'ring-emerald-500' },
  amber:   { text: 'text-amber-600 dark:text-amber-400',     bar: 'bg-amber-500',   dot: 'bg-amber-500',   ring: 'ring-amber-500' },
  sky:     { text: 'text-sky-600 dark:text-sky-400',         bar: 'bg-sky-500',     dot: 'bg-sky-500',     ring: 'ring-sky-500' },
  rose:    { text: 'text-rose-600 dark:text-rose-400',       bar: 'bg-rose-500',    dot: 'bg-rose-500',    ring: 'ring-rose-500' },
  slate:   { text: 'text-muted-foreground',                  bar: 'bg-muted-foreground', dot: 'bg-muted-foreground', ring: 'ring-muted-foreground' },
};

export default function SegmentFunnel({
  counts,
  activeKey,
}: {
  counts: TenderViewCounts | null;
  activeKey: string;
}) {
  const maxCount = counts
    ? Math.max(1, ...FUNNEL_VIEWS.map((v) => counts[v.countKey] ?? 0))
    : 1;

  const allView = TENDER_VIEWS.find((v) => v.key === 'all')!;

  return (
    <div>
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
        {FUNNEL_VIEWS.map((v) => {
          const tone = TONE[v.color];
          const count = counts ? counts[v.countKey] ?? 0 : null;
          const width = count != null ? Math.max(4, Math.round((count / maxCount) * 100)) : 0;
          const isActive = v.key === activeKey;
          // Only the highest-intent pile carries a colored count when inactive;
          // the rest stay muted so Pursue vs Set aside reads at a glance.
          const emphaticCount = isActive || v.key === 'to_bid';
          return (
            <NavLink
              key={v.key}
              to={viewHref(v.slug)}
              end={v.slug === ''}
              aria-label={`${v.label}${count != null ? `, ${count} tenders` : ''}`}
              className={cn(
                'group flex flex-col gap-2 rounded-2xl border bg-card px-4 py-3.5 text-left transition-all duration-200 hover:-translate-y-0.5 hover:shadow-card-hover',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1',
                isActive
                  ? cn('shadow-sm ring-1', tone.ring, 'border-transparent')
                  : 'border-border hover:border-foreground/20',
              )}
            >
              <div className="flex items-center gap-2">
                <span className={cn('h-2 w-2 shrink-0 rounded-full', tone.dot)} />
                <span className="text-[13px] font-semibold tracking-tight">{v.label}</span>
              </div>
              <span
                className={cn(
                  'text-2xl font-extrabold leading-none tabular-nums tracking-tight',
                  emphaticCount ? tone.text : 'text-muted-foreground',
                )}
              >
                {count != null ? count.toLocaleString() : '—'}
              </span>
              <div className="h-1 overflow-hidden rounded-full bg-muted/70">
                <div className={cn('h-full rounded-full transition-all', tone.bar)} style={{ width: `${width}%` }} />
              </div>
            </NavLink>
          );
        })}
      </div>
      <div className="mt-2 flex justify-end">
        <NavLink
          to={viewHref(allView.slug)}
          end
          className={({ isActive }) =>
            cn(
              'text-xs font-medium transition-colors',
              isActive ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
            )
          }
        >
          {counts ? `All ${counts.all.toLocaleString()} tenders →` : 'All tenders →'}
        </NavLink>
      </div>
    </div>
  );
}
