export default function SkeletonCard() {
  return (
    <div className="border border-border rounded-xl p-4 animate-pulse">
      {/* Top row: category badge + status */}
      <div className="flex items-center justify-between mb-3">
        <div className="h-5 w-20 bg-muted rounded-full" />
        <div className="h-4 w-16 bg-muted rounded" />
      </div>

      {/* Title */}
      <div className="h-4 w-3/4 bg-muted rounded mb-2" />
      <div className="h-4 w-1/2 bg-muted rounded mb-3" />

      {/* Description */}
      <div className="h-3 w-full bg-muted rounded mb-1" />
      <div className="h-3 w-2/3 bg-muted rounded mb-3" />

      {/* Bottom row */}
      <div className="flex items-center justify-between pt-2 border-t border-border">
        <div className="flex items-center gap-1">
          <div className="h-4 w-14 bg-muted rounded" />
          <div className="h-4 w-16 bg-muted rounded" />
        </div>
        <div className="h-3 w-6 bg-muted rounded" />
      </div>
    </div>
  );
}
