import { useEffect, useRef, useState } from 'react';
import { Download, FileSpreadsheet, Loader2 } from 'lucide-react';
import * as pdfjsLib from 'pdfjs-dist';
// Bundle the worker locally — a CDN URL would break the app wherever
// third-party network access is blocked.
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

import { API_BASE } from '../../lib/api';
import type { Artifact } from '../../types/command-center';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorker;

interface PreviewManifest {
  status: 'ready' | 'pending' | 'failed' | 'unavailable';
  pdf_url: string | null;
  page_count: number;
  sheets: { name: string; start_page: number }[];
  error: string | null;
}

/** Faithful preview of the generated workbook: one tab per worksheet over a
 *  LibreOffice-rendered PDF. Falls back to a plain table when no render exists
 *  (artifacts created before this feature, or a failed render). */
export function XlsxPreview({
  artifact,
  onDownload,
  downloading = false,
}: {
  artifact: Artifact;
  onDownload: () => void;
  /** The workbook is being fetched right now — say so on the button. */
  downloading?: boolean;
}) {
  const [manifest, setManifest] = useState<{ artifactId: number; data: PreviewManifest } | null>(
    null,
  );
  const [activeSheet, setActiveSheet] = useState(0);
  const [loading, setLoading] = useState(true);
  const [renderError, setRenderError] = useState(false);
  const [pollExhausted, setPollExhausted] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<any>(null);

  const sd: any = (artifact as any).structured_data || {};
  const rows: any[] = Array.isArray(sd.rows) ? sd.rows : [];
  const totalWithGst = sd.total_with_gst;

  // Only trust a manifest that belongs to the currently displayed artifact —
  // a stale manifest from the previous artifact must never be rendered.
  const currentManifest = manifest && manifest.artifactId === artifact.id ? manifest.data : null;

  const destroyDoc = () => {
    if (docRef.current) {
      try {
        docRef.current.destroy();
      } catch {
        // ignore — best-effort cleanup
      }
      docRef.current = null;
    }
  };

  // A new artifact means a new PDF (and a possibly-invalid previous tab
  // index) — reset both instead of reusing the stale cached document.
  useEffect(() => {
    destroyDoc();
    setActiveSheet(0);
    setRenderError(false);
    setPollExhausted(false);
  }, [artifact.id]);

  // Destroy the cached pdf.js document on unmount to free worker resources.
  useEffect(() => {
    return () => {
      destroyDoc();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        timeoutId = setTimeout(resolve, ms);
      });

    setLoading(true);
    (async () => {
      const POLL_INTERVAL_MS = 3000;
      const MAX_ATTEMPTS = 20;

      for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
        let body: PreviewManifest | null = null;
        try {
          const token = localStorage.getItem('drpl_token');
          const res = await fetch(
            `${API_BASE}/api/command-center/artifacts/${artifact.id}/preview`,
            { headers: { Authorization: `Bearer ${token}` } },
          );
          if (cancelled) return;
          body = await res.json();
          if (cancelled) return;
        } catch {
          if (!cancelled) setManifest(null);
          if (!cancelled && attempt === 0) setLoading(false);
          return;
        }

        if (!cancelled) setManifest({ artifactId: artifact.id, data: body! });
        if (cancelled) return;
        if (attempt === 0) setLoading(false);
        if (cancelled) return;

        if (body!.status !== 'pending') return;

        // Still pending: wait and retry, unless we've hit the last attempt.
        if (attempt < MAX_ATTEMPTS - 1) {
          await sleep(POLL_INTERVAL_MS);
          if (cancelled) return;
        } else if (!cancelled) {
          setPollExhausted(true);
        }
      }
    })();
    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, [artifact.id]);

  // Render the pages of the active sheet. Only trusts a manifest that
  // belongs to the current artifact — a stale manifest from a just-switched
  // artifact must never drive a fetch or a render.
  useEffect(() => {
    if (!manifest || manifest.artifactId !== artifact.id) return;
    const data = manifest.data;
    if (data.status !== 'ready' || !data.pdf_url) return;
    let cancelled = false;
    setRenderError(false);

    (async () => {
      try {
        const container = containerRef.current;
        if (!container) return;
        if (!docRef.current) {
          const loaded = await pdfjsLib.getDocument(data.pdf_url!).promise;
          if (cancelled || docRef.current) {
            try {
              await loaded.destroy();
            } catch {
              // Best-effort cleanup of an abandoned document; nothing to do.
            }
            if (cancelled) return;
          } else {
            docRef.current = loaded;
          }
        }
        if (cancelled || !docRef.current) return;

        const sheets = data.sheets;
        const start = sheets[activeSheet]?.start_page ?? 1;
        const end =
          activeSheet + 1 < sheets.length
            ? sheets[activeSheet + 1].start_page - 1
            : data.page_count;

        container.innerHTML = '';
        for (let p = start; p <= end; p++) {
          if (cancelled || !docRef.current) return;
          const page = await docRef.current.getPage(p);
          if (cancelled || !docRef.current) return;
          const viewport = page.getViewport({ scale: 1.4 });
          const canvas = document.createElement('canvas');
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          canvas.className = 'w-full h-auto border border-border rounded mb-3 bg-white';
          container.appendChild(canvas);
          if (cancelled || !docRef.current) return;
          await page.render({ canvasContext: canvas.getContext('2d')!, viewport }).promise;
          if (cancelled) return;
        }
      } catch (err) {
        // A cancelled/torn-down render (destroyed doc, unmounted container)
        // is a normal event on artifact switch, not a user-facing error.
        if (!cancelled) {
          console.warn('XlsxPreview render aborted', err);
          setRenderError(true);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [manifest, activeSheet, artifact.id]);

  const header = (
    <div className="rounded-xl border border-emerald-200 dark:border-emerald-500/20 bg-emerald-50/50 dark:bg-emerald-500/10 p-4 flex items-center gap-3">
      <div className="w-10 h-10 rounded-lg bg-card border border-emerald-200 dark:border-emerald-500/20 flex items-center justify-center">
        <FileSpreadsheet size={18} className="text-emerald-600 dark:text-emerald-400" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="font-semibold text-foreground truncate">{artifact.title}</p>
        <p className="text-xs text-muted-foreground">
          {currentManifest?.status === 'ready'
            ? `${currentManifest.sheets.length} sheet${currentManifest.sheets.length === 1 ? '' : 's'} · ${currentManifest.page_count} pages`
            : `${rows.length} line items${
                typeof totalWithGst === 'number'
                  ? ` · ₹${totalWithGst.toLocaleString('en-IN')} (incl. GST)`
                  : ''
              }`}
        </p>
      </div>
      <button
        onClick={onDownload}
        disabled={downloading}
        aria-busy={downloading}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-xs font-semibold hover:bg-emerald-700 transition-colors disabled:opacity-70 disabled:cursor-wait"
      >
        {downloading ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
        {downloading ? 'Preparing…' : 'Download .xlsx'}
      </button>
    </div>
  );

  if (loading) {
    return (
      <div className="space-y-4">
        {header}
        <div className="flex items-center gap-2 text-sm text-muted-foreground p-6 justify-center">
          <Loader2 size={16} className="animate-spin" />
          Loading preview…
        </div>
      </div>
    );
  }

  if (currentManifest?.status === 'ready' && currentManifest.pdf_url && !renderError) {
    return (
      <div className="space-y-3">
        {header}
        <div className="flex gap-1 overflow-x-auto border-b border-border pb-px">
          {currentManifest.sheets.map((s, i) => (
            <button
              key={s.name}
              onClick={() => setActiveSheet(i)}
              className={`px-3 py-1.5 text-xs font-medium whitespace-nowrap rounded-t-lg border border-b-0 transition-colors ${
                i === activeSheet
                  ? 'bg-card border-border text-foreground'
                  : 'bg-muted/40 border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {s.name}
            </button>
          ))}
        </div>
        <div ref={containerRef} className="max-h-[70vh] overflow-y-auto" />
      </div>
    );
  }

  // Fallback: no render available. Keep the previous table so nothing regresses.
  return (
    <div className="space-y-4">
      {header}
      {currentManifest?.status === 'pending' && (
        <p className="text-xs text-muted-foreground px-1">
          {pollExhausted
            ? 'Full spreadsheet preview is taking longer than expected. Showing the line items below — please reload to check again.'
            : 'Full spreadsheet preview is still being generated. Showing the line items below.'}
        </p>
      )}
      {currentManifest?.status === 'failed' && (
        <p className="text-xs text-amber-600 dark:text-amber-500 px-1">
          Full spreadsheet preview could not be generated. Showing the line items below —
          the downloaded file is unaffected.
        </p>
      )}
      {renderError && (
        <p className="text-xs text-amber-600 dark:text-amber-500 px-1">
          Could not display the spreadsheet preview. Showing the line items below —
          the downloaded file is unaffected.
        </p>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="min-w-full text-xs">
            <thead className="bg-muted/40 text-muted-foreground">
              <tr>
                <th className="px-2 py-2 text-left font-semibold">Description</th>
                <th className="px-2 py-2 text-left font-semibold">Category</th>
                <th className="px-2 py-2 text-right font-semibold">Qty</th>
                <th className="px-2 py-2 text-left font-semibold">Unit</th>
                <th className="px-2 py-2 text-right font-semibold">Rate (₹)</th>
                <th className="px-2 py-2 text-right font-semibold">Amount (₹)</th>
                <th className="px-2 py-2 text-right font-semibold">GST %</th>
                <th className="px-2 py-2 text-right font-semibold">Total (₹)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-t border-border">
                  <td className="px-2 py-2 align-top">{r.description}</td>
                  <td className="px-2 py-2 align-top">{r.category}</td>
                  <td className="px-2 py-2 text-right align-top">{r.qty ?? ''}</td>
                  <td className="px-2 py-2 align-top">{r.unit}</td>
                  <td className="px-2 py-2 text-right align-top">{r.rate ?? ''}</td>
                  <td className="px-2 py-2 text-right align-top">{r.amount ?? ''}</td>
                  <td className="px-2 py-2 text-right align-top">{r.gst_pct ?? ''}</td>
                  <td className="px-2 py-2 text-right align-top">{r.total ?? ''}</td>
                </tr>
              ))}
              {typeof sd.total_amount === 'number' && (
                <tr className="border-t-2 border-border bg-muted/40 font-semibold">
                  <td className="px-2 py-2" colSpan={5}>TOTAL</td>
                  <td className="px-2 py-2 text-right">{sd.total_amount.toLocaleString('en-IN')}</td>
                  <td />
                  <td className="px-2 py-2 text-right">
                    {typeof sd.total_with_gst === 'number' ? sd.total_with_gst.toLocaleString('en-IN') : ''}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
