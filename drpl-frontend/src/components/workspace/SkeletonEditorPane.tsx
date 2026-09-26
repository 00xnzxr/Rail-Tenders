export default function SkeletonEditorPane() {
  return (
    <div className="min-h-screen bg-muted/40 animate-pulse">
      {/* Header placeholder */}
      <div className="h-16 bg-card border-b" />

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {/* Top navigation */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="h-4 w-20 bg-muted rounded" />
            <div className="h-4 w-1 bg-muted rounded" />
            <div className="h-5 w-48 bg-muted rounded" />
          </div>
          <div className="flex items-center gap-3">
            <div className="h-7 w-24 bg-muted rounded-full" />
          </div>
        </div>

        {/* Info bar */}
        <div className="bg-card border rounded-xl mb-4 px-4 py-3">
          <div className="flex items-center gap-4">
            <div className="h-4 w-24 bg-muted rounded" />
            <div className="h-5 w-20 bg-muted rounded" />
            <div className="h-4 w-16 bg-muted rounded" />
            <div className="ml-auto flex items-center gap-1">
              <div className="h-6 w-16 bg-muted rounded-lg" />
              <div className="h-6 w-14 bg-muted rounded-lg" />
              <div className="h-6 w-16 bg-muted rounded-lg" />
              <div className="h-6 w-20 bg-muted rounded-lg" />
            </div>
          </div>
        </div>

        {/* Split pane */}
        <div className="flex gap-4">
          {/* Editor */}
          <div className="flex-1 min-w-0">
            <div className="bg-card border rounded-xl overflow-hidden">
              {/* Toolbar */}
              <div className="flex items-center gap-2 px-4 py-2 border-b">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="h-6 w-6 bg-muted rounded" />
                ))}
              </div>
              {/* Editor area */}
              <div className="p-6 space-y-3" style={{ minHeight: 480 }}>
                <div className="h-5 w-1/3 bg-muted rounded" />
                <div className="h-4 w-full bg-muted rounded" />
                <div className="h-4 w-5/6 bg-muted rounded" />
                <div className="h-4 w-2/3 bg-muted rounded" />
                <div className="h-4 w-0" />
                <div className="h-4 w-full bg-muted rounded" />
                <div className="h-4 w-4/5 bg-muted rounded" />
                <div className="h-4 w-1/2 bg-muted rounded" />
                <div className="h-4 w-0" />
                <div className="h-5 w-1/4 bg-muted rounded" />
                <div className="h-4 w-full bg-muted rounded" />
                <div className="h-4 w-3/4 bg-muted rounded" />
              </div>
            </div>

            {/* Action buttons */}
            <div className="flex items-center gap-3 mt-4">
              <div className="h-9 w-28 bg-muted rounded-lg" />
              <div className="h-9 w-28 bg-muted rounded-lg" />
              <div className="h-9 w-28 bg-muted rounded-lg" />
              <div className="h-9 w-44 bg-muted rounded-lg" />
            </div>
          </div>

          {/* Chat panel */}
          <div className="w-96 flex-shrink-0">
            <div className="bg-card border rounded-xl h-[620px] p-4">
              <div className="flex items-center gap-2 mb-4 pb-3 border-b">
                <div className="h-6 w-6 bg-muted rounded-full" />
                <div className="h-4 w-24 bg-muted rounded" />
              </div>
              <div className="space-y-3">
                <div className="h-12 w-3/4 bg-muted rounded-lg" />
                <div className="h-10 w-2/3 bg-muted rounded-lg ml-auto" />
                <div className="h-16 w-3/4 bg-muted rounded-lg" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
