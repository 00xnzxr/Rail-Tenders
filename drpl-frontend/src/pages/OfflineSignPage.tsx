import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Download, PenTool, Loader2, CheckCircle2, X } from 'lucide-react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import SignaturePlacementBoard from '../components/editor/SignaturePlacementBoard';
import {
  getOfflineDocument, getSignatures, updateOfflineDocumentPlacement,
  applyOfflineSignatures, downloadOfflineDocument, getOfflinePreviewPages,
  type OfflineDocument, type SignatureConfig,
} from '../lib/api';

// The 9 named presets the board + backend understand.
const PRESETS: { key: string; label: string }[] = [
  { key: 'top-left', label: 'TL' }, { key: 'top-center', label: 'TC' }, { key: 'top-right', label: 'TR' },
  { key: 'middle-left', label: 'ML' }, { key: 'middle-center', label: 'MC' }, { key: 'middle-right', label: 'MR' },
  { key: 'bottom-left', label: 'BL' }, { key: 'bottom-center', label: 'BC' }, { key: 'bottom-right', label: 'BR' },
];

export default function OfflineSignPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const docId = id ? parseInt(id, 10) : null;

  const [doc, setDoc] = useState<OfflineDocument | null>(null);
  const [signatures, setSignatures] = useState<any[]>([]);
  const [configs, setConfigs] = useState<SignatureConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [applying, setApplying] = useState(false);
  const [signed, setSigned] = useState(false);

  // Rasterised source PDF for the placement board.
  const [pagesImageUrl, setPagesImageUrl] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [pageHeightPx, setPageHeightPx] = useState<number | null>(null);
  const [pageWidthPx, setPageWidthPx] = useState<number | null>(null);
  // True when the server couldn't rasterise the PDF (e.g. poppler missing).
  // The board still works via presets; we surface a gentle hint.
  const [previewUnavailable, setPreviewUnavailable] = useState(false);

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  // --- Load doc + signatures + rasterised pages ---
  useEffect(() => {
    if (!docId) return;
    let cancelled = false;
    (async () => {
      // Step 1 — load the doc + signatures. This is the only fatal path: if it
      // fails there is nothing to edit.
      let loaded: OfflineDocument;
      try {
        setLoading(true);
        const [d, sigs] = await Promise.all([getOfflineDocument(docId), getSignatures()]);
        if (cancelled) return;
        loaded = d;
        setDoc(d);
        setSignatures(sigs);
        setConfigs(d.signatures || []);
        setSigned(d.has_signed_output);
      } catch (err: any) {
        if (!cancelled) setError(err.response?.data?.detail || 'Failed to load document');
        if (!cancelled) setLoading(false);
        return;
      }
      if (!cancelled) setLoading(false);

      // Step 2 — the visual page preview is a nicety, not a requirement. If
      // rasterisation fails (e.g. poppler missing), the board falls back to its
      // grid and the user can still place signatures via presets + page
      // selector, apply, and download. Never block the editor on this.
      try {
        const pages = await getOfflinePreviewPages(docId);
        if (cancelled) return;
        const url = URL.createObjectURL(pages.blob);
        objectUrlRef.current = url;
        setPagesImageUrl(url);
        setPageCount(pages.pageCount);
        setPageHeightPx(pages.pageHeightPx);
        setPageWidthPx(pages.pageWidthPx);
      } catch {
        if (!cancelled) {
          setPreviewUnavailable(true);
          // Fall back to the page count captured at upload time, if any.
          if (loaded.page_count) setPageCount(loaded.page_count);
        }
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, [docId]);

  // --- Debounced autosave of placement ---
  const scheduleSave = useCallback((next: SignatureConfig[]) => {
    if (!docId) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      updateOfflineDocumentPlacement(docId, { signatures: next }).catch(() => {
        setError('Failed to save placement');
      });
    }, 600);
  }, [docId]);

  const commitConfigs = (next: SignatureConfig[]) => {
    setConfigs(next);
    setSigned(false); // placement changed → needs re-apply
    scheduleSave(next);
  };

  // --- Signature config helpers (same shape as the generator) ---
  const toggleSignature = (sigId: number) => {
    const exists = configs.find((s) => s.signature_id === sigId);
    if (exists) {
      commitConfigs(configs.filter((s) => s.signature_id !== sigId));
      return;
    }
    const sig = signatures.find((s: any) => s.id === sigId);
    commitConfigs([
      ...configs,
      {
        signature_id: sigId,
        position: sig?.default_position || 'bottom-right',
        position_x: sig?.default_position_x,
        position_y: sig?.default_position_y,
        page: 'last',
      },
    ]);
  };

  const updateConfig = (sigId: number, updates: Partial<SignatureConfig>) => {
    commitConfigs(configs.map((s) => (s.signature_id === sigId ? { ...s, ...updates } : s)));
  };

  const isSelected = (sigId: number) => configs.some((s) => s.signature_id === sigId);
  const getConfig = (sigId: number) => configs.find((s) => s.signature_id === sigId);

  // --- Apply + download ---
  const flushSave = async () => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    if (docId) await updateOfflineDocumentPlacement(docId, { signatures: configs });
  };

  const handleApply = async () => {
    if (!docId) return;
    if (configs.length === 0) {
      setError('Place at least one signature first');
      return;
    }
    setApplying(true);
    setError('');
    try {
      await flushSave();
      await applyOfflineSignatures(docId);
      setSigned(true);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to apply signatures');
    } finally {
      setApplying(false);
    }
  };

  const handleDownload = async () => {
    if (!docId || !doc) return;
    try {
      const blob = await downloadOfflineDocument(docId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = signed ? `signed_${doc.title}` : doc.title;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Download failed');
    }
  };

  if (loading) {
    return <div className="flex-1 flex items-center justify-center h-96"><LoadingSpinner /></div>;
  }

  return (
    <div className="flex-1 overflow-auto bg-background">
      <Header title={doc?.title || 'Sign PDF'} subtitle="Place signatures, review the result, and download" />

      <div className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6 lg:py-8">
        <button
          onClick={() => navigate('/documents')}
          className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition"
        >
          <ArrowLeft size={15} /> Back to documents
        </button>

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400 rounded-lg px-4 py-3 text-sm flex items-center justify-between">
            {error}
            <button onClick={() => setError('')} className="text-red-500 hover:text-red-700"><X size={16} /></button>
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-[300px_minmax(0,1fr)] gap-6">
          {/* ── Left rail: controls ── */}
          <div className="space-y-4 order-2 lg:order-1">
            <div className="bg-card rounded-xl border border-border p-5">
              <div className="flex items-center gap-2 mb-3">
                <PenTool size={16} className="text-accent" />
                <h3 className="text-sm font-semibold text-foreground">Signatures</h3>
              </div>

              {signatures.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No signatures yet.{' '}
                  <button onClick={() => navigate('/signatures')} className="text-accent hover:underline">
                    Create one
                  </button>{' '}
                  to place it on this PDF.
                </p>
              ) : (
                <div className="space-y-2">
                  {signatures.map((sig: any) => {
                    const selected = isSelected(sig.id);
                    const cfg = getConfig(sig.id);
                    return (
                      <div
                        key={sig.id}
                        className={`rounded-lg border p-3 transition ${
                          selected ? 'border-accent/40 bg-accent/5' : 'border-border'
                        }`}
                      >
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={selected}
                            onChange={() => toggleSignature(sig.id)}
                            className="accent-[var(--accent)]"
                          />
                          <span className="text-sm font-medium text-foreground">{sig.name}</span>
                          {sig.designation && (
                            <span className="text-xs text-muted-foreground">· {sig.designation}</span>
                          )}
                        </label>

                        {selected && cfg && (
                          <div className="mt-3 space-y-2 pl-6">
                            {/* Preset position grid (the quick path) */}
                            <div>
                              <p className="text-[11px] text-muted-foreground mb-1">Quick position</p>
                              <div className="grid grid-cols-3 gap-1 w-28">
                                {PRESETS.map((p) => (
                                  <button
                                    key={p.key}
                                    onClick={() => updateConfig(sig.id, {
                                      position: p.key, position_x: undefined, position_y: undefined,
                                    })}
                                    title={p.key}
                                    className={`text-[10px] py-1 rounded border transition ${
                                      cfg.position === p.key
                                        ? 'bg-accent text-accent-foreground border-accent'
                                        : 'border-border text-muted-foreground hover:bg-muted'
                                    }`}
                                  >
                                    {p.label}
                                  </button>
                                ))}
                              </div>
                            </div>

                            {/* Page selector */}
                            <div>
                              <p className="text-[11px] text-muted-foreground mb-1">Page</p>
                              <select
                                value={String(cfg.page)}
                                onChange={(e) => {
                                  const v = e.target.value;
                                  const page = ['first', 'last', 'all'].includes(v) ? v : parseInt(v, 10);
                                  updateConfig(sig.id, { page });
                                }}
                                className="text-xs border border-border rounded px-2 py-1 bg-card focus:outline-none focus:ring-1 focus:ring-ring"
                              >
                                <option value="first">First page</option>
                                <option value="last">Last page</option>
                                <option value="all">All pages</option>
                                {Array.from({ length: pageCount || 1 }).map((_, i) => (
                                  <option key={i + 1} value={i + 1}>Page {i + 1}</option>
                                ))}
                              </select>
                            </div>

                            {cfg.position === 'custom' && cfg.position_x != null && cfg.position_y != null && (
                              <p className="text-[10px] text-muted-foreground">
                                Custom: {cfg.position_x.toFixed(1)}mm × {cfg.position_y.toFixed(1)}mm from bottom-left
                              </p>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Actions */}
            <div className="bg-card rounded-xl border border-border p-5 space-y-3">
              <button
                onClick={handleApply}
                disabled={applying || configs.length === 0}
                className="w-full px-4 py-2.5 text-sm bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 flex items-center justify-center gap-2 disabled:opacity-60"
              >
                {applying ? <Loader2 size={16} className="animate-spin" /> : <PenTool size={16} />}
                {applying ? 'Applying…' : 'Apply signatures & save'}
              </button>

              <button
                onClick={handleDownload}
                disabled={!signed}
                className="w-full px-4 py-2.5 text-sm border border-border text-foreground rounded-lg hover:bg-muted/40 flex items-center justify-center gap-2 disabled:opacity-50"
              >
                <Download size={16} /> Download signed PDF
              </button>

              {signed && (
                <p className="text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1 justify-center">
                  <CheckCircle2 size={13} /> Signed copy ready
                </p>
              )}
            </div>
          </div>

          {/* ── Right pane: visual placement board (the hero) ── */}
          <div className="order-1 lg:order-2">
            <div className="bg-card rounded-xl border border-border p-4 sticky top-4">
              <h3 className="text-sm font-semibold text-foreground mb-3">Placement</h3>
              {previewUnavailable && (
                <p className="text-[11px] text-amber-600 dark:text-amber-400 mb-2">
                  PDF preview is unavailable on this server, but you can still place
                  signatures using the quick-position buttons and page selector, then
                  apply and download.
                </p>
              )}
              <SignaturePlacementBoard
                signatures={signatures}
                signatureConfigs={configs}
                onUpdateConfig={updateConfig}
                overridePageCount={pageCount}
                pdfPagesImageUrl={pagesImageUrl}
                pdfPagesPageHeightPx={pageHeightPx}
                pdfPagesPageWidthPx={pageWidthPx}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
