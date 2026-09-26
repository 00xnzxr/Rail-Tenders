import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { getRecentCostings, type RecentCosting } from '../../lib/api';
import { formatCurrency } from '../../lib/formatters';

export default function LatestCostings() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<RecentCosting[] | null>(null);

  useEffect(() => {
    getRecentCostings(5).then(setRows).catch(() => setRows([]));
  }, []);

  return (
    <div className="bg-card border border-border rounded-xl shadow-card overflow-hidden transition-shadow duration-200 hover:shadow-card-hover">
      <div className="px-5 py-3.5 border-b border-border flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
        <h3 className="text-sm font-bold text-foreground">Latest costings</h3>
      </div>
      {rows == null ? (
        <div className="p-5 space-y-3">
          {[0, 1, 2].map((i) => <div key={i} className="h-10 bg-muted/50 rounded animate-pulse" />)}
        </div>
      ) : rows.length === 0 ? (
        <p className="p-5 text-sm text-muted-foreground">No costings yet. Open a tender's Command Center to build one.</p>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map((c) => (
            <li key={`${c.tender_id}-${c.updated_at}`}>
              <button
                onClick={() => navigate(`/tenders/${c.tender_id}/command-center`)}
                className="w-full flex items-center gap-3 px-5 py-3 text-left hover:bg-muted/50 transition-colors group"
              >
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-foreground truncate">{c.title}</span>
                  <span className="block text-xs text-muted-foreground">
                    {c.grand_total != null ? formatCurrency(c.grand_total) : '—'}
                    {c.margin_pct != null && <> · {Math.round(c.margin_pct)}% margin</>}
                  </span>
                </span>
                <span className={`shrink-0 px-2 py-0.5 rounded-md text-[0.68rem] font-bold border ${
                  c.status === 'finalized'
                    ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/25'
                    : 'bg-muted text-muted-foreground border-border'
                }`}>{c.status}</span>
                <ArrowRight size={16} className="shrink-0 text-muted-foreground group-hover:text-accent transition-colors" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
