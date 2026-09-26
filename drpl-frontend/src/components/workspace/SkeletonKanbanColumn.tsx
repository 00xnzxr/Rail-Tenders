import SkeletonCard from './SkeletonCard';

interface Props {
  cardCount?: number;
}

export default function SkeletonKanbanColumn({ cardCount = 3 }: Props) {
  return (
    <div className="w-56 flex-shrink-0 animate-pulse">
      {/* Column header */}
      <div className="flex items-center justify-between px-3 py-2 mb-3">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-muted" />
          <div className="h-4 w-20 bg-muted rounded" />
        </div>
        <div className="h-4 w-5 bg-muted rounded-full" />
      </div>

      {/* Skeleton cards */}
      <div className="space-y-2">
        {Array.from({ length: cardCount }).map((_, i) => (
          <div key={i} className="border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-1.5 h-1.5 rounded-full bg-muted" />
              <div className="h-3 flex-1 bg-muted rounded" />
            </div>
            <div className="flex items-center gap-1">
              <div className="h-3 w-12 bg-muted rounded" />
              <div className="h-3 w-10 bg-muted rounded" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
