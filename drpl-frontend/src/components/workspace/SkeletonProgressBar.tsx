export default function SkeletonProgressBar() {
  return (
    <div className="bg-card border rounded-xl p-4 animate-pulse">
      {/* Main progress bar */}
      <div className="flex items-center gap-3 mb-3">
        <div className="h-4 w-28 bg-muted rounded" />
        <div className="flex-1 h-3 bg-muted rounded-full" />
        <div className="h-4 w-10 bg-muted rounded" />
      </div>

      {/* Category pills */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-muted" />
          <div className="h-3 w-24 bg-muted rounded" />
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-muted" />
          <div className="h-3 w-20 bg-muted rounded" />
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-muted" />
          <div className="h-3 w-22 bg-muted rounded" />
        </div>
      </div>
    </div>
  );
}
