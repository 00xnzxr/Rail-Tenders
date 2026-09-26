import { useState, useEffect, useCallback, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import {
  Loader2, RefreshCw, Download, ChevronDown, FileText, FileType, Package, Eye, Plus, Stamp,
} from 'lucide-react';
import AnnexurePageBlock from './AnnexurePageBlock';
import CombinedAnnexurePreview from './CombinedAnnexurePreview';
import { useLetterheadAndSignatureLists } from './useLetterheadAndSignatureLists';
import {
  getWorkspace, exportAnnexuresPdf, exportAnnexuresDocx, exportAnnexuresZip,
  createManualAnnexure, getAnnexureLetterhead, setAnnexureLetterhead,
} from '../../lib/api';
import type { WorkspaceItem } from '../../types/workspace';

interface Props {
  tenderId: number;
}

const ANNEXURE_PREFIX = 'annexure_finder:';

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * Single-tab "document pages" view of every annexure for a tender. All
 * annexures stack vertically and are filled top-to-bottom; the toolbar exports
 * them all in one shot (merged PDF / combined DOCX / both) — no manual merging.
 */
export default function AnnexuresStackedView({ tenderId }: Props) {
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [previewOpen, setPreviewOpen] = useState(true);
  const [previewVersion, setPreviewVersion] = useState(0);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const [exporting, setExporting] = useState<null | 'pdf' | 'docx' | 'zip'>(null);
  // Manual add-annexure modal state.
  const [addOpen, setAddOpen] = useState(false);
  const [addTitle, setAddTitle] = useState('');
  const [adding, setAdding] = useState(false);
  // Newly-added annexure id — expand + scroll to it, same as a deep-link target.
  const [expandId, setExpandId] = useState<number | null>(null);

  // Tender-wide default letterhead for all annexures. null = none (bare pages).
  const [letterheadId, setLetterheadId] = useState<number | null>(null);
  const [savingLetterhead, setSavingLetterhead] = useState(false);

  const { templates, signatures } = useLetterheadAndSignatureLists();

  // Deep-link target: /…/annexures#annexure-<itemId>. The target block starts
  // expanded and we scroll to it once the list has loaded.
  const location = useLocation();
  const hashTargetId = (() => {
    const m = /^#annexure-(\d+)$/.exec(location.hash || '');
    return m ? Number(m[1]) : null;
  })();

  // Map of itemId → flush fn registered by each block, so we can save every
  // block before a combined preview render or export.
  const flushMapRef = useRef<Map<number, () => Promise<void>>>(new Map());
  const registerFlush = useCallback((itemId: number, fn: (() => Promise<void>) | null) => {
    if (fn) flushMapRef.current.set(itemId, fn);
    else flushMapRef.current.delete(itemId);
  }, []);

  const flushAll = useCallback(async () => {
    await Promise.all(Array.from(flushMapRef.current.values()).map((fn) => fn().catch(() => {})));
  }, []);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const overview = await getWorkspace(tenderId);
      const annexures: WorkspaceItem[] = (overview.items || [])
        .filter((it: WorkspaceItem) => (it.source_section || '').startsWith(ANNEXURE_PREFIX))
        .sort((a: WorkspaceItem, b: WorkspaceItem) =>
          (a.display_order - b.display_order) || (a.id - b.id));
      setItems(annexures);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load annexures');
    } finally {
      setLoading(false);
    }
  }, [tenderId]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Load the tender's current annexure letterhead default.
  useEffect(() => {
    let cancelled = false;
    getAnnexureLetterhead(tenderId)
      .then((r) => { if (!cancelled) setLetterheadId(r.default_letterhead_id ?? null); })
      .catch(() => { /* non-fatal: picker just shows "No letterhead" */ });
    return () => { cancelled = true; };
  }, [tenderId]);

  const handleLetterheadChange = async (next: number | null) => {
    const previous = letterheadId;
    setLetterheadId(next);           // optimistic — the preview refresh reads this
    setSavingLetterhead(true);
    setError('');
    try {
      await setAnnexureLetterhead(tenderId, next);
      setPreviewVersion((v) => v + 1); // re-render the merged preview with/without it
    } catch (err: any) {
      setLetterheadId(previous);     // roll back so the UI matches the server
      setError(err.response?.data?.detail || 'Failed to update letterhead');
    } finally {
      setSavingLetterhead(false);
    }
  };

  // Scroll the deep-linked annexure into view once the list is rendered.
  // Re-runs if the hash changes (e.g. clicking another annexure card while the
  // stacked view is already open).
  useEffect(() => {
    if (loading || hashTargetId == null || !items.some((it) => it.id === hashTargetId)) return;
    const timer = setTimeout(() => {
      document
        .getElementById(`annexure-${hashTargetId}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 150);
    return () => clearTimeout(timer);
  }, [loading, hashTargetId, items]);

  const handleSaved = useCallback(() => {
    // A block saved — let the merged preview pick up the change (debounced
    // inside the preview component).
    setPreviewVersion((v) => v + 1);
  }, []);

  const handleDeleted = useCallback((itemId: number) => {
    setItems((prev) => prev.filter((it) => it.id !== itemId));
    setPreviewVersion((v) => v + 1); // merged preview/export no longer includes it
  }, []);

  const handleAddAnnexure = async () => {
    const title = addTitle.trim();
    if (!title || adding) return;
    setAdding(true);
    setError('');
    try {
      const created = await createManualAnnexure(tenderId, title);
      setAddOpen(false);
      setAddTitle('');
      await fetchData();
      setExpandId(created.checklist_item_id);
      setPreviewVersion((v) => v + 1);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to add annexure');
    } finally {
      setAdding(false);
    }
  };

  // Scroll a freshly-added annexure into view once it's rendered.
  useEffect(() => {
    if (expandId == null || !items.some((it) => it.id === expandId)) return;
    const timer = setTimeout(() => {
      document.getElementById(`annexure-${expandId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 150);
    return () => clearTimeout(timer);
  }, [expandId, items]);

  const runExport = async (kind: 'pdf' | 'docx' | 'zip') => {
    setExportMenuOpen(false);
    setExporting(kind);
    setError('');
    try {
      await flushAll(); // make sure every annexure's latest edits are persisted
      let blob: Blob;
      let ext: string;
      if (kind === 'pdf') { blob = await exportAnnexuresPdf(tenderId); ext = 'pdf'; }
      else if (kind === 'docx') { blob = await exportAnnexuresDocx(tenderId); ext = 'docx'; }
      else { blob = await exportAnnexuresZip(tenderId); ext = 'zip'; }
      triggerDownload(blob, `annexures.${ext}`);
    } catch (err: any) {
      // Blob error responses need to be read as text to surface the detail.
      let detail = 'Export failed';
      try {
        const data = err?.response?.data;
        if (data instanceof Blob) detail = JSON.parse(await data.text())?.detail || detail;
        else detail = data?.detail || detail;
      } catch { /* keep default */ }
      setError(detail);
    } finally {
      setExporting(null);
    }
  };

  const approvedCount = items.filter((it) => it.review_status === 'approved').length;

  const renderAddModal = () => {
    if (!addOpen) return null;
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
        <div className="bg-card rounded-xl shadow-xl w-full max-w-md p-6">
          <h3 className="text-lg font-semibold text-foreground mb-1">Add annexure</h3>
          <p className="text-sm text-muted-foreground mb-4">
            Add a blank annexure for a form the bidder must submit that isn't in the tender
            documents. You can fill its content below after it's created.
          </p>
          <label className="block text-xs font-medium text-muted-foreground mb-1">Annexure title</label>
          <input
            type="text"
            value={addTitle}
            onChange={(e) => setAddTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleAddAnnexure(); }}
            placeholder="e.g. Manufacturer's Authorization Form"
            autoFocus
            className="w-full border border-border rounded-lg px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
          <div className="flex justify-end gap-3 mt-5">
            <button
              onClick={() => { setAddOpen(false); setAddTitle(''); }}
              className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleAddAnnexure}
              disabled={!addTitle.trim() || adding}
              className="inline-flex items-center gap-1.5 px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700 transition-colors disabled:opacity-50"
            >
              {adding ? <><Loader2 size={14} className="animate-spin" /> Adding…</> : <><Plus size={14} /> Add annexure</>}
            </button>
          </div>
        </div>
      </div>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20 text-sm text-muted-foreground">
        <Loader2 size={18} className="animate-spin mr-2" /> Loading annexures…
      </div>
    );
  }

  if (!items.length) {
    return (
      <>
        <div className="text-center py-20">
          <FileText size={40} className="mx-auto text-muted-foreground/50 mb-3" />
          <h2 className="text-lg font-semibold text-foreground mb-1">No annexures yet</h2>
          <p className="text-sm text-muted-foreground max-w-md mx-auto">
            Run annexure extraction from the tender documents, or add one manually for a
            form that isn't in the documents.
          </p>
          <div className="mt-4 flex items-center justify-center gap-2">
            <button
              onClick={() => { setAddTitle(''); setAddOpen(true); }}
              className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700"
            >
              <Plus size={14} /> Add annexure
            </button>
            <button
              onClick={fetchData}
              className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg border border-border bg-card hover:bg-muted/40"
            >
              <RefreshCw size={14} /> Refresh
            </button>
          </div>
        </div>
        {renderAddModal()}
      </>
    );
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <div className="min-w-0">
          <h2 className="text-base font-bold text-foreground">All Annexures ({items.length})</h2>
          <p className="text-xs text-muted-foreground">
            {approvedCount} of {items.length} approved · fill top-to-bottom, then export in one go
          </p>
        </div>

        <div className="ml-auto flex items-center gap-2">
          {/* Tender-wide letterhead applied to every annexure on export/preview.
              Individual annexures can override this from their own block —
              e.g. a bank guarantee bond belongs on the bank's letterhead. */}
          <label className="inline-flex items-center gap-1.5 text-sm">
            <Stamp size={14} className="text-muted-foreground" />
            <span className="text-xs text-muted-foreground hidden sm:inline">Letterhead</span>
            <select
              value={letterheadId ?? ''}
              disabled={savingLetterhead}
              onChange={(e) => handleLetterheadChange(e.target.value ? Number(e.target.value) : null)}
              title="Applied to every annexure in the exported PDF"
              className="text-sm border border-border rounded-lg px-2 py-1.5 bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-60 max-w-[13rem]"
            >
              <option value="">No letterhead</option>
              {templates.map((t: any) => (
                <option key={t.id} value={t.id}>
                  {t.name}{t.is_default ? ' (default)' : ''}
                </option>
              ))}
            </select>
            {savingLetterhead && <Loader2 size={13} className="animate-spin text-muted-foreground" />}
          </label>

          <button
            type="button"
            onClick={() => { setAddTitle(''); setAddOpen(true); }}
            className="inline-flex items-center gap-1.5 text-sm font-medium px-3 py-1.5 rounded-lg border border-border bg-card hover:bg-muted/40 text-foreground"
          >
            <Plus size={14} /> Add annexure
          </button>
          <button
            type="button"
            onClick={() => setPreviewOpen((v) => !v)}
            className="inline-flex items-center gap-1.5 text-sm font-medium px-3 py-1.5 rounded-lg border border-border bg-card hover:bg-muted/40 text-foreground"
          >
            <Eye size={14} /> {previewOpen ? 'Hide preview' : 'Show preview'}
          </button>

          {/* Export All dropdown */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setExportMenuOpen((v) => !v)}
              disabled={exporting !== null}
              className="inline-flex items-center gap-1.5 text-sm font-semibold px-3 py-1.5 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60"
            >
              {exporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
              {exporting ? 'Exporting…' : 'Export All'}
              <ChevronDown size={14} />
            </button>
            {exportMenuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setExportMenuOpen(false)} />
                <div className="absolute right-0 mt-1 z-20 w-52 bg-card border rounded-lg shadow-lg py-1">
                  <button onClick={() => runExport('pdf')} className="w-full flex items-center gap-2 px-3 py-2 text-sm text-foreground hover:bg-muted/40 text-left">
                    <FileText size={14} /> Merged PDF
                  </button>
                  <button onClick={() => runExport('docx')} className="w-full flex items-center gap-2 px-3 py-2 text-sm text-foreground hover:bg-muted/40 text-left">
                    <FileType size={14} /> Combined DOCX
                  </button>
                  <button onClick={() => runExport('zip')} className="w-full flex items-center gap-2 px-3 py-2 text-sm text-foreground hover:bg-muted/40 text-left">
                    <Package size={14} /> PDF + DOCX (.zip)
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="flex gap-4">
        {/* Stacked annexure pages */}
        <div className="flex-1 min-w-0 space-y-4">
          {items.map((item, idx) => (
            <AnnexurePageBlock
              key={item.id}
              tenderId={tenderId}
              item={item}
              index={idx + 1}
              signatures={signatures}
              templates={templates}
              defaultLetterheadId={letterheadId}
              defaultExpanded={idx < 3 || item.id === hashTargetId || item.id === expandId}
              forceExpand={item.id === hashTargetId || item.id === expandId}
              registerFlush={registerFlush}
              onSaved={handleSaved}
              onDeleted={handleDeleted}
            />
          ))}
        </div>

        {/* Combined live preview */}
        {previewOpen && (
          <div className="w-96 flex-shrink-0">
            <div className="h-[680px] sticky top-4">
              <CombinedAnnexurePreview
                tenderId={tenderId}
                refreshKey={previewVersion}
                beforeFetch={flushAll}
              />
            </div>
          </div>
        )}
      </div>

      {renderAddModal()}
    </div>
  );
}
