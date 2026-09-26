import { useEffect, useRef, useState } from 'react';
import { Loader2, RefreshCw, AlertTriangle, FileText } from 'lucide-react';
import { previewCombinedAnnexures } from '../../lib/api';

interface Props {
  tenderId: number;
  /** Bump to force a fresh combined render (e.g. after any annexure saves). */
  refreshKey: number;
  /** Called before every fetch — lets the parent flush pending block saves so
   *  the merged preview reflects the latest edits across all annexures. */
  beforeFetch?: () => Promise<void>;
}

/**
 * Live merged PDF preview of every annexure in a tender, stacked in order.
 * Mirrors DocumentPdfPreview but hits the combined-annexures endpoint so the
 * user sees exactly what "Export All (PDF)" will produce.
 */
export default function CombinedAnnexurePreview({ tenderId, refreshKey, beforeFetch }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [manualVersion, setManualVersion] = useState(0);

  const currentUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const isInitialOrManual = refreshKey === 0 || manualVersion > 0;
    const delay = isInitialOrManual ? 0 : 700;
    const timer = setTimeout(() => {
      (async () => {
        try {
          if (beforeFetch) await beforeFetch();
        } catch {
          // Non-fatal — block save errors surface in their own block.
        }
        if (cancelled) return;
        setLoading(true);
        setError(null);
        try {
          const { blob, pageCount: pc } = await previewCombinedAnnexures(tenderId);
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          if (currentUrlRef.current) URL.revokeObjectURL(currentUrlRef.current);
          currentUrlRef.current = url;
          setBlobUrl(url);
          setPageCount(pc);
        } catch (err: any) {
          if (cancelled) return;
          setError(err?.response?.data?.detail || 'Preview failed');
        } finally {
          if (!cancelled) setLoading(false);
        }
      })();
    }, delay);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [tenderId, refreshKey, manualVersion]);

  useEffect(() => {
    return () => {
      if (currentUrlRef.current) {
        URL.revokeObjectURL(currentUrlRef.current);
        currentUrlRef.current = null;
      }
    };
  }, []);

  return (
    <div className="bg-card border rounded-xl overflow-hidden h-full flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b bg-muted/40">
        <span className="text-xs font-semibold text-foreground flex items-center gap-1.5">
          <FileText size={12} /> Combined preview
          {pageCount ? <span className="text-muted-foreground font-normal">· {pageCount}p</span> : null}
        </span>
        <button
          type="button"
          onClick={() => setManualVersion((v) => v + 1)}
          disabled={loading}
          title="Refresh combined preview"
          className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          {loading ? 'Rendering…' : 'Refresh'}
        </button>
      </div>

      <div className="flex-1 relative bg-muted">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-card/60 z-10">
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 size={14} className="animate-spin" /> Rendering all annexures…
            </span>
          </div>
        )}
        {error && (
          <div className="absolute inset-0 flex items-center justify-center p-4">
            <div className="flex items-start gap-2 text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg px-3 py-2 max-w-full">
              <AlertTriangle size={14} className="flex-shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          </div>
        )}
        {!error && blobUrl && (
          <iframe src={blobUrl} title="Combined annexures preview" className="w-full h-full border-0" />
        )}
        {!error && !blobUrl && !loading && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
            No preview yet.
          </div>
        )}
      </div>
    </div>
  );
}
