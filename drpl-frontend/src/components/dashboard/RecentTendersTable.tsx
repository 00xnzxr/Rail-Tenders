import { useNavigate } from 'react-router-dom';
import PortalBadge from '../ui/PortalBadge';
import StatusBadge from '../ui/StatusBadge';
import { formatCurrency, formatDate } from '../../lib/formatters';
import type { Tender } from '../../types/tender';

function AIScoreBadge({ score }: { score: number | null }) {
  if (score == null) return <span className="text-xs text-muted-foreground/50">—</span>;
  const pct = Math.round(score * 100);
  const color =
    pct >= 70 ? 'text-emerald-600 bg-emerald-50 dark:text-emerald-400 dark:bg-emerald-500/15' :
    pct >= 40 ? 'text-amber-600 bg-amber-50 dark:text-amber-400 dark:bg-amber-500/15' :
                'text-red-600 bg-red-50 dark:text-red-400 dark:bg-red-500/15';
  return <span className={`inline-block px-2 py-0.5 rounded-md text-xs font-semibold ${color}`}>{pct}%</span>;
}

export default function RecentTendersTable({ tenders }: { tenders: Tender[] }) {
  const navigate = useNavigate();
  return (
    <div className="bg-card rounded-xl border border-border overflow-hidden shadow-card">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border">
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Title</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Portal</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Status</th>
              <th className="px-4 py-3 text-right text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Value</th>
              <th className="px-4 py-3 text-center text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">AI Score</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold text-muted-foreground uppercase tracking-widest">Closing</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {tenders.map((t) => (
              <tr key={t.id} onClick={() => navigate(`/tenders/${t.id}`)}
                  className="hover:bg-muted/50 cursor-pointer transition-colors">
                <td className="px-4 py-3 text-sm text-foreground font-medium max-w-xs truncate">{t.title}</td>
                <td className="px-4 py-3"><PortalBadge portal={t.portal} /></td>
                <td className="px-4 py-3"><StatusBadge status={t.status} /></td>
                <td className="px-4 py-3 text-sm text-muted-foreground text-right tabular-nums">{formatCurrency(t.estimated_value)}</td>
                <td className="px-4 py-3 text-center"><AIScoreBadge score={t.ai_relevance_score} /></td>
                <td className="px-4 py-3 text-sm text-muted-foreground tabular-nums">{formatDate(t.closing_date)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
