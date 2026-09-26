import { useEffect, useRef, useState } from 'react';
import { Loader2, RefreshCw, AlertTriangle, FileText } from 'lucide-react';
import { previewWorkspaceDocument } from '../../lib/api';

interface Props {
  tenderId: number;
  itemId: number;
  /**
   * Increment this in the parent to force a fresh preview render.
   * Letterhead / signature / orientation changes should bump it AFTER the
   * PATCH succeeds so the backend has the new state.
   */
  refreshKey: number;
  /**
   * Optional async hook called before every fetch — lets the parent flush
   * any pending content auto-save so the preview reflects the latest text.
   */
  beforeFetch?: () => Promise<void>;
  /**
   * Fires with the actual rendered page count after every successful
   * preview fetch (from the `X-PDF-Page-Count` response header). Used to
   * line up the signature placement board's page boundaries with the real
   * PDF when letterhead margins push content onto extra pages.
   */
  onPageCount?: (count: number | null) => void;
}

export default function DocumentPdfPreview({
  tenderId,
  itemId,
  refreshKey,
  beforeFetch,
  onPageCount,
}: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  // Bumped by the manual Refresh button. Combined with refreshKey from props
  // so either source triggers a fetch.
  const [manualVersion, setManualVersion] = useState(0);

  const currentUrlRef = useRef<string | null>(null);
  // Keep the latest onPageCount in a ref so the fetch effect (which only
  // depends on refresh triggers) always calls the most recent callback
  // without re-firing whenever the parent passes a fresh inline closure.
  const onPageCountRef = useRef(onPageCount);
  useEffect(() => {
    onPageCountRef.current = onPageCount;
  }, [onPageCount]);

  useEffect(() => {
    let cancelled = false;
    // Debounce so a fast drag on the signature placement board (which fires
    // many onUpdateConfig events per second) doesn't queue a PDF render per
    // frame. The debounce only applies to prop-driven refreshKey bumps; the
    // initial mount + manual Refresh click fire immediately by setting
    // delay=0 below for those cases.
    const isInitialOrManual = refreshKey === 0 || manualVersion > 0;
    const delay = isInitialOrManual ? 0 : 700;
    const timer = setTimeout(() => {
      (async () => {
        try {
          if (beforeFetch) await beforeFetch();
        } catch {
          // Non-fatal: parent's save failed; surface it via its own error state.
        }
        if (cancelled) return;
        setLoading(true);
        setError(null);
        try {
          const { blob, pageCount } = await previewWorkspaceDocument(tenderId, itemId);
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          // Revoke previous blob URL to avoid the browser holding the old PDF
          // bytes in memory for the lifetime of the tab.
          if (currentUrlRef.current) {
            URL.revokeObjectURL(currentUrlRef.current);
          }
          currentUrlRef.current = url;
          setBlobUrl(url);
          onPageCountRef.current?.(pageCount);
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
  }, [tenderId, itemId, refreshKey, manualVersion]);

  // Final cleanup on unmount.
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
          <FileText size={12} /> PDF Preview
        </span>
        <button
          type="button"
          onClick={() => setManualVersion((v) => v + 1)}
          disabled={loading}
          title="Refresh preview"
          className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {loading ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <RefreshCw size={12} />
          )}
          {loading ? 'Rendering…' : 'Refresh'}
        </button>
      </div>

      <div className="flex-1 relative bg-muted">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-card/60 z-10">
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 size={14} className="animate-spin" /> Rendering preview…
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
          <iframe
            src={blobUrl}
            title="Document PDF preview"
            className="w-full h-full border-0"
          />
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
