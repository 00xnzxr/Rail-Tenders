import type { TenderStats } from '../../types/tender';

interface Props {
  stats: TenderStats | null;
}

export default function HeroSummary({ stats }: Props) {
  if (!stats) {
    return <div className="h-24 bg-card border border-border rounded-xl animate-pulse" />;
  }

  // Scoring hasn't produced any relevance yet — steer the user to scoring instead.
  if (stats.avg_relevance == null || (stats.promising_count ?? 0) === 0) {
    const pending = stats.total_tenders - (stats.analyzed_tenders ?? 0);
    return (
      <div className="bg-card border border-border border-l-4 border-l-accent rounded-xl p-6 shadow-card">
        <p className="text-[0.62rem] uppercase tracking-[0.15em] text-muted-foreground font-bold mb-2">Where things stand</p>
        <p className="text-xl leading-relaxed text-foreground">
          Scoring is still finding your best matches
          {pending > 0 ? <> — <span className="font-bold tabular-nums">{pending.toLocaleString()}</span> tenders waiting.</> : '.'}
        </p>
      </div>
    );
  }

  const promising = stats.promising_count ?? 0;
  const open = stats.promising_open_count ?? 0;
  const soon = stats.closing_soon_count ?? 0;
  const costed = stats.with_costing_count ?? 0;

  return (
    <div className="bg-card border border-border border-l-4 border-l-accent rounded-xl p-6 shadow-card">
      <p className="text-[0.62rem] uppercase tracking-[0.15em] text-muted-foreground font-bold mb-2">Where things stand</p>
      <p className="text-xl sm:text-2xl leading-snug text-foreground font-medium">
        You have{' '}
        <span className="font-extrabold text-accent tabular-nums">{promising.toLocaleString()}</span>{' '}
        promising {promising === 1 ? 'tender' : 'tenders'}.{' '}
        <span className="text-muted-foreground font-normal text-lg sm:text-xl">
          <span className="font-bold text-foreground tabular-nums">{open.toLocaleString()}</span> still open,{' '}
          <span className="font-bold text-foreground tabular-nums">{soon.toLocaleString()}</span> {soon === 1 ? 'closes' : 'close'} this week, and{' '}
          <span className="font-bold text-foreground tabular-nums">{costed.toLocaleString()}</span> already {costed === 1 ? 'has a costing' : 'have costings'}.
        </span>
      </p>
    </div>
  );
}
