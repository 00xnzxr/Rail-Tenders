import { useState, useEffect, useRef, useCallback } from 'react';
import {
  Loader2, CheckCircle2, ChevronDown, ChevronRight, FileSearch,
  RectangleHorizontal, RectangleVertical, Clock, Edit3, Eye, XCircle, Trash2, Stamp,
} from 'lucide-react';
import RichTextEditor from '../editor/RichTextEditor';
import {
  getDocumentWorkspace, saveDocumentContent, setDocumentOrientation,
  reExtractAnnexure, getSignatureImageDataUri, deleteWorkspaceItem,
  setItemLetterhead,
} from '../../lib/api';
import type { DocumentWorkspaceDetail, WorkspaceItem } from '../../types/workspace';

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; icon: typeof Clock }> = {
  not_started: { label: 'Not Started', color: 'text-muted-foreground', bg: 'bg-muted', icon: Clock },
  drafting: { label: 'Drafting', color: 'text-accent', bg: 'bg-accent/10', icon: Edit3 },
  in_review: { label: 'In Review', color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-500/15', icon: Eye },
  approved: { label: 'Approved', color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-50 dark:bg-emerald-500/15', icon: CheckCircle2 },
  rejected: { label: 'Rejected', color: 'text-red-600 dark:text-red-400', bg: 'bg-red-50 dark:bg-red-500/15', icon: XCircle },
};

interface Props {
  tenderId: number;
  item: WorkspaceItem;
  /** 1-based index for the "PAGE n" label. */
  index: number;
  signatures: any[];
  /** Letterhead templates available for the per-annexure override. */
  templates?: any[];
  /** Tender-wide default letterhead id, shown as the "Inherit" option's target. */
  defaultLetterheadId?: number | null;
  /** Whether this block starts expanded (editor mounted). */
  defaultExpanded: boolean;
  /** When true (e.g. this annexure is the deep-link target), expand even if it
   *  was collapsed — covers re-targeting while the view is already open. */
  forceExpand?: boolean;
  /** Register/unregister a flush fn so the parent can save every block before
   *  rendering the combined preview / exporting. */
  registerFlush: (itemId: number, fn: (() => Promise<void>) | null) => void;
  /** Called after a successful save so the parent can refresh the merged preview. */
  onSaved: () => void;
  /** Called after this annexure is deleted so the parent can drop it from the list. */
  onDeleted: (itemId: number) => void;
}

/**
 * One annexure rendered as a "page" in the stacked annexures view. Owns its own
 * fetch + autosave + orientation/re-extract so each annexure behaves like the
 * old per-item editor, but inline and stacked. Content fetched lazily on expand.
 */
export default function AnnexurePageBlock({
  tenderId, item, index, signatures, templates = [], defaultLetterheadId = null,
  defaultExpanded, forceExpand, registerFlush, onSaved, onDeleted,
}: Props) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [deleting, setDeleting] = useState(false);

  // Expand when this block becomes the deep-link target while already mounted.
  useEffect(() => {
    if (forceExpand) setExpanded(true);
  }, [forceExpand]);
  const [detail, setDetail] = useState<DocumentWorkspaceDetail | null>(null);
  const [editorContent, setEditorContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved'>('idle');
  const [orientation, setOrientation] = useState<'portrait' | 'landscape'>('portrait');
  const [status, setStatus] = useState(item.review_status);
  const [reExtracting, setReExtracting] = useState(false);
  const [error, setError] = useState('');
  // Per-annexure letterhead override. Tri-state, encoded for the <select>:
  //   'inherit' → use the tender default
  //   'none'    → explicitly no letterhead (e.g. a bank's guarantee bond)
  //   '<id>'    → force a specific template
  const [letterheadChoice, setLetterheadChoice] = useState<string>('inherit');
  const [savingLetterhead, setSavingLetterhead] = useState(false);

  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const contentRef = useRef('');
  const dirtyRef = useRef(false);

  const identifier = (item.source_section || '').replace(/^annexure_finder:/, '') || item.item_name;

  const loadDetail = useCallback(async () => {
    if (loaded || loading) return;
    setLoading(true);
    try {
      const ws = await getDocumentWorkspace(tenderId, item.id);
      setDetail(ws);
      const html = ws.workspace.draft_content_html || '';
      setEditorContent(html);
      contentRef.current = html;
      setOrientation(ws.workspace.page_orientation || 'portrait');
      setStatus(ws.workspace.review_status);
      setLetterheadChoice(
        ws.workspace.letterhead_disabled ? 'none'
          : ws.workspace.letterhead_template_id != null
            ? String(ws.workspace.letterhead_template_id)
            : 'inherit',
      );
      setLoaded(true);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load annexure');
    } finally {
      setLoading(false);
    }
  }, [tenderId, item.id, loaded, loading]);

  // Fetch when first expanded.
  useEffect(() => {
    if (expanded && !loaded) loadDetail();
  }, [expanded, loaded, loadDetail]);

  const doSave = useCallback(async () => {
    if (!dirtyRef.current) return;
    if (saveTimerRef.current) { clearTimeout(saveTimerRef.current); saveTimerRef.current = null; }
    setSaveStatus('saving');
    try {
      const result = await saveDocumentContent(tenderId, item.id, contentRef.current, null);
      dirtyRef.current = false;
      setSaveStatus('saved');
      if (result?.review_status) setStatus(result.review_status);
      setTimeout(() => setSaveStatus('idle'), 1500);
      onSaved();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save');
      setSaveStatus('idle');
    }
  }, [tenderId, item.id, onSaved]);

  // Expose a flush fn to the parent (save-on-demand before export/preview).
  useEffect(() => {
    registerFlush(item.id, doSave);
    return () => registerFlush(item.id, null);
  }, [item.id, doSave, registerFlush]);

  const handleContentChange = (html: string) => {
    setEditorContent(html);
    contentRef.current = html;
    dirtyRef.current = true;
    setSaveStatus('idle');
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => { doSave(); }, 3000);
  };

  const handleOrientationToggle = async () => {
    const next = orientation === 'landscape' ? 'portrait' : 'landscape';
    const prev = orientation;
    setOrientation(next);
    try {
      await setDocumentOrientation(tenderId, item.id, next);
      onSaved();
    } catch (err: any) {
      setOrientation(prev);
      setError(err.response?.data?.detail || 'Failed to change orientation');
    }
  };

  const handleLetterheadChange = async (next: string) => {
    const previous = letterheadChoice;
    setLetterheadChoice(next);
    setSavingLetterhead(true);
    setError('');
    try {
      await setItemLetterhead(tenderId, item.id, {
        disabled: next === 'none',
        letterheadTemplateId: next === 'none' || next === 'inherit' ? null : Number(next),
      });
      onSaved(); // refresh the merged preview so the change is visible
    } catch (err: any) {
      setLetterheadChoice(previous);
      setError(err.response?.data?.detail || 'Failed to update letterhead');
    } finally {
      setSavingLetterhead(false);
    }
  };

  const handleReExtract = async () => {
    if (!confirm('Re-extract this annexure from the tender PDF? This overwrites the current draft content.')) return;
    setReExtracting(true);
    setError('');
    try {
      await reExtractAnnexure(tenderId, item.id);
      setLoaded(false);
      await loadDetail();
      onSaved();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Re-extraction failed');
    } finally {
      setReExtracting(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`Delete "${identifier}"? This removes the annexure from the submission set. This cannot be undone.`)) return;
    setDeleting(true);
    setError('');
    try {
      await deleteWorkspaceItem(tenderId, item.id);
      registerFlush(item.id, null); // stop the parent from trying to flush a deleted block
      onDeleted(item.id);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete annexure');
      setDeleting(false);
    }
  };

  const canReExtract = !['in_review', 'approved'].includes(status);
  const statusCfg = STATUS_CONFIG[status] || STATUS_CONFIG.not_started;
  const StatusIcon = statusCfg.icon;
  const isLandscape = orientation === 'landscape';

  return (
    <div id={`annexure-${item.id}`} className="bg-card border rounded-xl overflow-hidden scroll-mt-24">
      {/* Block header */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b bg-muted/40">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-2 min-w-0 flex-1 text-left"
        >
          {expanded ? <ChevronDown size={16} className="flex-shrink-0 text-muted-foreground" />
                    : <ChevronRight size={16} className="flex-shrink-0 text-muted-foreground" />}
          <span className="text-[11px] font-mono text-muted-foreground flex-shrink-0">PAGE {index}</span>
          <span className="text-sm font-semibold text-foreground truncate">{identifier}</span>
          <span className="text-xs text-muted-foreground truncate hidden sm:inline">· {item.item_name}</span>
        </button>

        <span className={`inline-flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded-full flex-shrink-0 ${statusCfg.bg} ${statusCfg.color}`}>
          <StatusIcon size={11} />
          {statusCfg.label}
        </span>

        {saveStatus !== 'idle' && (
          <span className={`flex items-center gap-1 text-[11px] flex-shrink-0 ${saveStatus === 'saved' ? 'text-emerald-500' : 'text-muted-foreground'}`}>
            {saveStatus === 'saving' ? <><Loader2 size={11} className="animate-spin" /> Saving…</> : <><CheckCircle2 size={11} /> Saved</>}
          </span>
        )}

        <button
          type="button"
          onClick={handleDelete}
          disabled={deleting}
          title="Delete this annexure"
          aria-label={`Delete ${identifier}`}
          className="flex-shrink-0 p-1.5 rounded-lg text-muted-foreground hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-500/15 transition-colors disabled:opacity-50"
        >
          {deleting ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
        </button>
      </div>

      {expanded && (
        <div className="p-3">
          {error && (
            <div className="mb-2 p-2 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded text-xs text-red-700 dark:text-red-400">
              {error}
            </div>
          )}

          {loading && !loaded ? (
            <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
              <Loader2 size={16} className="animate-spin mr-2" /> Loading annexure…
            </div>
          ) : (
            <>
              {/* Per-block toolbar */}
              <div className="flex items-center gap-2 mb-2">
                <button
                  type="button"
                  onClick={handleOrientationToggle}
                  title={`Switch to ${isLandscape ? 'portrait' : 'landscape'}`}
                  className="inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg border border-border bg-card hover:bg-muted/40 text-foreground"
                >
                  {isLandscape ? <><RectangleHorizontal size={13} /> Landscape</> : <><RectangleVertical size={13} /> Portrait</>}
                </button>
                {canReExtract && (
                  <button
                    type="button"
                    onClick={handleReExtract}
                    disabled={reExtracting}
                    title="Re-run the annexure extractor and overwrite this draft"
                    className="inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg border border-amber-200 dark:border-amber-500/20 bg-amber-50 dark:bg-amber-500/15 hover:bg-amber-100 text-amber-800 dark:text-amber-400 disabled:opacity-60"
                  >
                    {reExtracting ? <><Loader2 size={13} className="animate-spin" /> Re-extracting…</> : <><FileSearch size={13} /> Re-extract</>}
                  </button>
                )}
                {/* Per-annexure letterhead override. Defaults to inheriting the
                    tender-wide choice; set to "None" for forms that belong on a
                    third party's letterhead (e.g. a bank guarantee bond). */}
                <label className="inline-flex items-center gap-1" title="Letterhead for this annexure only">
                  <Stamp size={13} className="text-muted-foreground" />
                  <select
                    value={letterheadChoice}
                    disabled={savingLetterhead}
                    onChange={(e) => handleLetterheadChange(e.target.value)}
                    className="text-xs border border-border rounded-lg px-1.5 py-1 bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-60 max-w-[11rem]"
                  >
                    <option value="inherit">
                      {defaultLetterheadId
                        ? `Inherit (${templates.find((t: any) => t.id === defaultLetterheadId)?.name || 'tender default'})`
                        : 'Inherit (none set)'}
                    </option>
                    <option value="none">No letterhead</option>
                    {templates.map((t: any) => (
                      <option key={t.id} value={String(t.id)}>{t.name}</option>
                    ))}
                  </select>
                  {savingLetterhead && <Loader2 size={12} className="animate-spin text-muted-foreground" />}
                </label>

                {detail && (
                  <span className="ml-auto text-[11px] text-muted-foreground">
                    Tip: insert your signature via the editor toolbar.
                  </span>
                )}
              </div>

              <div className={`bg-card border rounded-lg overflow-hidden ${isLandscape ? 'max-w-full' : ''}`}>
                <RichTextEditor
                  value={editorContent}
                  onChange={handleContentChange}
                  placeholder="Fill in this annexure…"
                  minHeight={360}
                  signatures={signatures}
                  onResolveSignature={async (sigId, kind) => {
                    const r = await getSignatureImageDataUri(sigId, kind);
                    return { dataUri: r.data_uri, name: r.name, designation: r.designation };
                  }}
                  onSignatureInserted={async () => {
                    dirtyRef.current = true;
                    await doSave();
                  }}
                />
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
