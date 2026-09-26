import type { WorkspaceStats } from '../../types/workspace';

interface Props {
  stats: WorkspaceStats;
}

export default function WorkspaceProgressBar({ stats }: Props) {
  const { total_items, completion_percent, by_category } = stats;

  if (total_items === 0) return null;

  return (
    <div className="space-y-2">
      {/* Main progress bar */}
      <div className="flex items-center gap-3">
        <div className="flex-1 h-3 bg-muted rounded-full overflow-hidden">
          <div
            className="h-full bg-emerald-500 rounded-full transition-all duration-500"
            style={{ width: `${completion_percent}%` }}
          />
        </div>
        <span className="text-sm font-semibold text-foreground w-12 text-right">
          {completion_percent}%
        </span>
      </div>

      {/* Category breakdown */}
      <div className="flex gap-4 text-xs text-muted-foreground">
        {Object.entries(by_category).map(([cat, catStats]) => {
          const config = CATEGORY_COLORS[cat] || CATEGORY_COLORS.standard;
          return (
            <span key={cat} className="flex items-center gap-1">
              <span className={`w-2 h-2 rounded-full ${config.dot}`} />
              <span className="capitalize">{cat}:</span>
              <span className="font-medium text-foreground">
                {catStats.completed}/{catStats.total}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

const CATEGORY_COLORS: Record<string, { dot: string }> = {
  standard: { dot: 'bg-muted-foreground/40' },
  generated: { dot: 'bg-accent/100' },
  analysis: { dot: 'bg-amber-500' },
};
