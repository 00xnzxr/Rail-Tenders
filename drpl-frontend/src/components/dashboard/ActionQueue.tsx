import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, CalendarClock, Calculator, Search } from 'lucide-react';
import { getTenders } from '../../lib/api';
import type { Tender } from '../../types/tender';
import { differenceInDays, parseISO } from 'date-fns';

type QueueItem = { tender: Tender; reason: string; icon: React.ReactNode };

const PROMISING = 0.7;

export default function ActionQueue() {
  const navigate = useNavigate();
  const [items, setItems] = useState<QueueItem[] | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const soon = new Date();
        soon.setDate(soon.getDate() + 7);
        // Priority 1: promising + open + closing within 7 days.
        const closing = await getTenders({
          status: 'open', score_min: PROMISING,
          closing_before: soon.toISOString(), sort_by: 'closing_date', limit: 5,
        } as any);
        // Priority 2: promising + open, most relevant first (fills remaining slots).
        const promising = await getTenders({
          status: 'open', score_min: PROMISING, sort_by: 'relevance', limit: 10,
        } as any);

        const seen = new Set<number>();
        const out: QueueItem[] = [];
        for (const t of closing.items) {
          if (seen.has(t.id) || !t.closing_date) continue;
          seen.add(t.id);
          const d = differenceInDays(parseISO(t.closing_date), new Date());
          out.push({
            tender: t,
            reason: d <= 0 ? 'Closes today — review now' : `Review — closes in ${d} day${d === 1 ? '' : 's'}`,
            icon: <CalendarClock size={16} className="text-amber-600 dark:text-amber-400" />,
          });
        }
        for (const t of promising.items) {
          if (seen.has(t.id) || out.length >= 6) continue;
          seen.add(t.id);
          out.push({
            tender: t,
            reason: 'Start costing',
            icon: <Calculator size={16} className="text-emerald-600 dark:text-emerald-400" />,
          });
        }
        // Fallback: if scoring is behind and nothing promising surfaced, suggest reviewing recent open tenders.
        if (out.length === 0) {
          const recent = await getTenders({ status: 'open', sort_by: 'created_at', limit: 6 } as any);
          for (const t of recent.items) {
            out.push({
              tender: t,
              reason: 'Review',
              icon: <Search size={16} className="text-muted-foreground" />,
            });
          }
        }
        setItems(out);
      } catch {
        setItems([]);
      }
    })();
  }, []);

  return (
    <div className="bg-card border border-border rounded-xl shadow-card overflow-hidden transition-shadow duration-200 hover:shadow-card-hover">
      <div className="px-5 py-3.5 border-b border-border flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-accent" />
        <h3 className="text-sm font-bold text-foreground">Do this next</h3>
      </div>
      {items == null ? (
        <div className="p-5 space-y-3">
          {[0, 1, 2].map((i) => <div key={i} className="h-10 bg-muted/50 rounded animate-pulse" />)}
        </div>
      ) : items.length === 0 ? (
        <p className="p-5 text-sm text-muted-foreground">Nothing needs attention right now. 🎉</p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map(({ tender, reason, icon }) => (
            <li key={tender.id}>
              <button
                onClick={() => navigate(`/tenders/${tender.id}/command-center`)}
                className="w-full flex items-center gap-3 px-5 py-3 text-left hover:bg-muted/50 transition-colors group"
              >
                <span className="shrink-0">{icon}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-foreground truncate">{tender.title}</span>
                  <span className="block text-xs text-muted-foreground">{reason}</span>
                </span>
                <ArrowRight size={16} className="shrink-0 text-muted-foreground group-hover:text-accent transition-colors" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
