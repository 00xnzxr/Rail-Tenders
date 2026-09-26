import { Sparkles, Target, CalendarClock, FileText } from 'lucide-react';
import type { TenderStats } from '../../types/tender';

interface Props {
  stats: TenderStats | null;
}

function Card({ icon, value, label, tone, emphasized }: {
  icon: React.ReactNode; value: number | string; label: string; tone: string; emphasized?: boolean;
}) {
  return (
    <div className={`bg-card border rounded-xl p-5 shadow-card transition-shadow duration-200 hover:shadow-card-hover ${
      emphasized ? 'border-accent/40 ring-1 ring-accent/15' : 'border-border'
    }`}>
      <div className={`inline-flex items-center justify-center w-9 h-9 rounded-lg mb-4 ${tone}`}>
        {icon}
      </div>
      <p className="text-[2rem] font-extrabold leading-none tabular-nums text-foreground">{value}</p>
      <p className="text-xs font-semibold text-muted-foreground mt-2 tracking-tight">{label}</p>
    </div>
  );
}

export default function MeaningfulStats({ stats }: Props) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <Card
        icon={<Sparkles size={18} className="text-accent" />}
        value={stats?.promising_count ?? '—'}
        label="Promising · ≥70% match"
        tone="bg-accent/10"
        emphasized
      />
      {/* "Ready to bid" was a second view of the same pile as Promising — both
          now resolve to the AI-score tier, so showing both would print the same
          number twice. Costings is the genuinely different signal. */}
      <Card
        icon={<Target size={18} className="text-emerald-600 dark:text-emerald-400" />}
        value={stats?.with_costing_count ?? '—'}
        label="With costings"
        tone="bg-emerald-500/10"
      />
      <Card
        icon={<CalendarClock size={18} className="text-amber-600 dark:text-amber-400" />}
        value={stats?.closing_soon_count ?? '—'}
        label="Closing this week"
        tone="bg-amber-500/10"
      />
      <Card
        icon={<FileText size={18} className="text-muted-foreground" />}
        value={stats?.total_tenders ?? '—'}
        label="Live tenders"
        tone="bg-muted"
      />
      {stats?.bifurcation && <Bifurcation b={stats.bifurcation} />}
    </div>
  );
}

/** Where the rest of the table went. Without this, a headline falling from
 *  1,649 to 269 reads as data loss rather than as honest recategorisation. */
function Bifurcation({ b }: { b: NonNullable<TenderStats['bifurcation']> }) {
  const parts = [
    { n: b.live, label: 'live' },
    { n: b.expired, label: 'expired' },
    { n: b.archived, label: 'archived' },
    ...(b.below_threshold ? [{ n: b.below_threshold, label: 'below value threshold' }] : []),
  ];
  return (
    <p className="col-span-2 lg:col-span-4 -mt-1 text-xs text-muted-foreground tabular-nums">
      {parts.map((p, i) => (
        <span key={p.label}>
          {i > 0 && <span className="mx-1.5 opacity-50">·</span>}
          <span className="font-medium text-foreground">{p.n.toLocaleString()}</span> {p.label}
        </span>
      ))}
      <span className="mx-1.5 opacity-50">·</span>
      {b.all_rows.toLocaleString()} total scraped
    </p>
  );
}
