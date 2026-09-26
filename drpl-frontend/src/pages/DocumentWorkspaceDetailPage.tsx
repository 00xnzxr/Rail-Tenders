import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  ArrowLeft, Save, CheckCircle2, Loader2,
  Clock, Edit3, Eye, XCircle, Wand2, Sparkles, RefreshCw,
  RectangleHorizontal, RectangleVertical, FileSearch,
} from 'lucide-react';
import Header from '../components/layout/Header';
import RichTextEditor from '../components/editor/RichTextEditor';
import SkeletonEditorPane from '../components/workspace/SkeletonEditorPane';
import DocumentInfoBar from '../components/workspace/DocumentInfoBar';
import DocumentPdfPreview from '../components/workspace/DocumentPdfPreview';
import { useLetterheadAndSignatureLists } from '../components/workspace/useLetterheadAndSignatureLists';
import {
  getTenderById, getDocumentWorkspace, saveDocumentContent,
  finalizeWorkspaceDocument, generateDocumentWithAgent,
  enhanceDocumentWithAgent, updateDocumentWorkspace,
  getWorkspace, setDocumentOrientation, reExtractAnnexure,
  getSignatureImageDataUri,
} from '../lib/api';
import type { TenderDetail } from '../types/tender';
import type { DocumentWorkspaceDetail, WorkspaceItem } from '../types/workspace';

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; icon: typeof Clock }> = {
  not_started: { label: 'Not Started', color: 'text-muted-foreground', bg: 'bg-muted', icon: Clock },
  drafting: { label: 'Drafting', color: 'text-accent', bg: 'bg-accent/10', icon: Edit3 },
  in_review: { label: 'In Review', color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-500/15', icon: Eye },
  approved: { label: 'Approved', color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-50 dark:bg-emerald-500/15', icon: CheckCircle2 },
  rejected: { label: 'Rejected', color: 'text-red-600 dark:text-red-400', bg: 'bg-red-50 dark:bg-red-500/15', icon: XCircle },
};

export default function DocumentWorkspaceDetailPage() {
  const { id, itemId } = useParams<{ id: string; itemId: string }>();
  const navigate = useNavigate();
  const tenderId = Number(id);
  const checklistItemId = Number(itemId);

  const [tender, setTender] = useState<TenderDetail | null>(null);
  const [detail, setDetail] = useState<DocumentWorkspaceDetail | null>(null);
  const [editorContent, setEditorContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [error, setError] = useState('');
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved'>('idle');
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [enhancing, setEnhancing] = useState(false);
  const [enhancePrompt, setEnhancePrompt] = useState('');
  const [showEnhanceModal, setShowEnhanceModal] = useState(false);
  const [allItems, setAllItems] = useState<WorkspaceItem[]>([]);
  const [reExtracting, setReExtracting] = useState(false);
  // PDF preview pane visibility. Auto-hides when the user switches to
  // landscape so the editor gets the full container width for wide tables.
  // The user can re-open via the header toggle.
  const [previewPanelOpen, setPreviewPanelOpen] = useState(true);
  // Bumped after each letterhead / signature / orientation PATCH succeeds so
  // the PDF preview pane re-fetches the rendered document. Editor content
  // changes do NOT bump this — too expensive to re-render PDF on each
  // keystroke. The preview pane's manual Refresh button covers content edits.
  const [previewVersion, setPreviewVersion] = useState(0);
  // Set after a successful Finalize so the user gets in-page confirmation
  // (instead of having to bounce to the Documents tab to see the result).
  const [finalizedDocId, setFinalizedDocId] = useState<number | null>(null);
  // Signature list for the editor's "Insert signature" toolbar dropdown.
  // Cached via the module-level hook so swapping workspace docs in the
  // same session doesn't refetch.
  const { signatures } = useLetterheadAndSignatureLists();

  // Auto-save debounce timer
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      const [t, ws] = await Promise.all([
        getTenderById(tenderId),
        getDocumentWorkspace(tenderId, checklistItemId),
      ]);
      setTender(t);
      setDetail(ws);
      setEditorContent(ws.workspace.draft_content_html || '');
      // Default preview panel to collapsed when the document is landscape
      // so the editor gets the full width for wide tables.
      setPreviewPanelOpen(ws.workspace.page_orientation !== 'landscape');

      // Load all items for dependency selection
      try {
        const overview = await getWorkspace(tenderId);
        setAllItems(overview.items || []);
      } catch {
        // Non-critical
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load document workspace');
    } finally {
      setLoading(false);
    }
  }, [tenderId, checklistItemId]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Keyboard shortcut: Ctrl+S to save
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });

  const handleContentChange = (html: string) => {
    setEditorContent(html);
    setHasUnsavedChanges(true);
    setSaveStatus('idle');

    // Auto-save after 3s of inactivity
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      doSave(html);
    }, 3000);
  };

  const doSave = async (html?: string) => {
    const content = html ?? editorContent;
    if (!content && !detail?.workspace.draft_content_html) return;

    setSaving(true);
    setSaveStatus('saving');
    try {
      const result = await saveDocumentContent(tenderId, checklistItemId, content, null);
      setSaveStatus('saved');
      setHasUnsavedChanges(false);
      // Update local state
      if (detail) {
        setDetail({
          ...detail,
          workspace: {
            ...detail.workspace,
            draft_content_html: content,
            content_version: result.content_version,
            review_status: result.review_status,
          },
        });
      }
      // Reset saved indicator after 2s
      setTimeout(() => setSaveStatus('idle'), 2000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save');
      setSaveStatus('idle');
    } finally {
      setSaving(false);
    }
  };

  const handleSave = () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    doSave();
  };

  const handleFinalize = async () => {
    if (hasUnsavedChanges) await doSave();

    setFinalizing(true);
    setError('');
    setFinalizedDocId(null);
    try {
      const result = await finalizeWorkspaceDocument(tenderId, checklistItemId);
      // Update local state
      if (detail) {
        setDetail({
          ...detail,
          workspace: {
            ...detail.workspace,
            review_status: 'approved',
          },
        });
      }
      // Surface in-page confirmation so the user doesn't have to navigate to
      // the Documents tab to verify the PDF was generated.
      if (result?.generated_document_id) {
        setFinalizedDocId(result.generated_document_id);
      } else {
        setFinalizedDocId(-1); // success but no id surfaced
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to finalize document');
    } finally {
      setFinalizing(false);
    }
  };

  const handleGenerate = async () => {
    setGenerating(true);
    setError('');
    try {
      const result = await generateDocumentWithAgent(tenderId, checklistItemId);
      setEditorContent(result.content_html || '');
      if (detail) {
        setDetail({
          ...detail,
          workspace: {
            ...detail.workspace,
            draft_content_html: result.content_html,
            content_version: result.content_version,
            review_status: 'drafting',
          },
        });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Generation failed');
    } finally {
      setGenerating(false);
    }
  };

  const handleEnhance = async () => {
    if (!enhancePrompt.trim()) return;
    setEnhancing(true);
    setShowEnhanceModal(false);
    setError('');
    try {
      // Save current content first
      if (hasUnsavedChanges) await doSave();
      const result = await enhanceDocumentWithAgent(tenderId, checklistItemId, enhancePrompt);
      setEditorContent(result.content_html || '');
      setEnhancePrompt('');
      if (detail) {
        setDetail({
          ...detail,
          workspace: {
            ...detail.workspace,
            draft_content_html: result.content_html,
            content_version: result.content_version,
          },
        });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Enhancement failed');
    } finally {
      setEnhancing(false);
    }
  };

  const handleAgentAssign = async (agentKey: string | null) => {
    try {
      await updateDocumentWorkspace(tenderId, checklistItemId, { agent_key: agentKey });
      if (detail) {
        setDetail({
          ...detail,
          workspace: { ...detail.workspace, agent_key: agentKey },
        });
      }
    } catch {
      // Ignore
    }
  };

  const handleTemplateChange = async (templateId: number | null) => {
    try {
      await updateDocumentWorkspace(tenderId, checklistItemId, { format_template_id: templateId });
      // Refetch to get updated format_template object
      const ws = await getDocumentWorkspace(tenderId, checklistItemId);
      setDetail(ws);
    } catch {
      // Ignore
    }
  };

  const handleDependenciesChange = async (deps: number[]) => {
    try {
      await updateDocumentWorkspace(tenderId, checklistItemId, { depends_on: deps });
      if (detail) {
        setDetail({
          ...detail,
          workspace: { ...detail.workspace, depends_on: deps },
        });
      }
    } catch {
      // Ignore
    }
  };

  const handleNotesChange = async (notes: string) => {
    try {
      await updateDocumentWorkspace(tenderId, checklistItemId, { notes });
      if (detail) {
        setDetail({
          ...detail,
          workspace: { ...detail.workspace, notes },
        });
      }
    } catch {
      // Ignore
    }
  };

  const handleSignaturesChange = async (configs: any[]) => {
    if (!detail) return;
    const prev = detail;
    // Optimistic update
    setDetail({
      ...detail,
      workspace: { ...detail.workspace, signatures_json: configs },
    });
    try {
      await updateDocumentWorkspace(tenderId, checklistItemId, { signatures_json: configs });
      setPreviewVersion((v) => v + 1);
    } catch (err: any) {
      setDetail(prev);
      setError(err.response?.data?.detail || 'Failed to update signatures');
    }
  };

  const handleLetterheadChange = async (templateId: number | null) => {
    if (!detail) return;
    const prev = detail;
    // Optimistic update
    setDetail({
      ...detail,
      workspace: { ...detail.workspace, letterhead_template_id: templateId },
    });
    try {
      await updateDocumentWorkspace(tenderId, checklistItemId, {
        letterhead_template_id: templateId,
      });
      setPreviewVersion((v) => v + 1);
    } catch (err: any) {
      setDetail(prev);
      setError(err.response?.data?.detail || 'Failed to update letterhead');
    }
  };

  const handleOrientationChange = async (next: 'portrait' | 'landscape') => {
    if (!detail) return;
    if (detail.workspace.page_orientation === next) return;
    const prev = detail;
    // Optimistic update
    setDetail({
      ...detail,
      workspace: { ...detail.workspace, page_orientation: next },
    });
    // Collapse preview by default when switching to landscape so the editor
    // gets the full width for wide tables; re-open by default on portrait.
    setPreviewPanelOpen(next === 'portrait');
    try {
      await setDocumentOrientation(tenderId, checklistItemId, next);
      setPreviewVersion((v) => v + 1);
    } catch (err: any) {
      setDetail(prev);
      setError(err.response?.data?.detail || 'Failed to change orientation');
    }
  };

  const handleOrientationToggle = () => {
    if (!detail) return;
    const next = detail.workspace.page_orientation === 'landscape' ? 'portrait' : 'landscape';
    handleOrientationChange(next);
  };

  const handleReExtract = async () => {
    if (!detail) return;
    if (!confirm(
      'Re-extract this annexure from the tender PDF? This overwrites the current draft content with a fresh copy.'
    )) return;
    setReExtracting(true);
    setError('');
    try {
      await reExtractAnnexure(tenderId, checklistItemId);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Re-extraction failed');
    } finally {
      setReExtracting(false);
    }
  };

  if (loading) return <SkeletonEditorPane />;
  if (!detail) return <div className="text-center py-12 text-muted-foreground">Document workspace not found.</div>;

  const { item, workspace, format_template } = detail;
  const status = STATUS_CONFIG[workspace.review_status] || STATUS_CONFIG.not_started;
  const StatusIcon = status.icon;
  const isLandscape = workspace.page_orientation === 'landscape';
  const isAnnexureDoc = (item.source_section || '').startsWith('annexure_finder:');
  const canReExtract = isAnnexureDoc && !['in_review', 'approved'].includes(workspace.review_status);
  // Widen the main container when landscape so the editor has room for wide tables.
  const mainMaxWidth = isLandscape ? 'max-w-[1600px]' : 'max-w-7xl';

  return (
    <div className="min-h-screen bg-background">
      <Header title="Prepare document" subtitle="Review, edit, and approve this tender document" />
      <main className={`${mainMaxWidth} mx-auto px-4 sm:px-6 lg:px-8 py-6`}>
        {/* Top navigation */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate(`/tenders/${tenderId}/command-center`)}
              className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              <ArrowLeft size={16} />
              Ask DRPL
            </button>
            <span className="text-muted-foreground/50">|</span>
            <h1 className="text-lg font-bold text-foreground line-clamp-1">
              {item.item_name}
            </h1>
          </div>

          <div className="flex items-center gap-3">
            {/* Orientation toggle */}
            <button
              type="button"
              onClick={handleOrientationToggle}
              title={`Switch to ${isLandscape ? 'portrait' : 'landscape'} orientation`}
              className="inline-flex items-center gap-1.5 text-sm font-medium px-3 py-1 rounded-lg border border-border bg-card hover:bg-muted/40 text-foreground"
            >
              {isLandscape
                ? <><RectangleHorizontal size={14} /> Landscape</>
                : <><RectangleVertical size={14} /> Portrait</>}
            </button>

            {/* Re-extract (annexures only, and only when unlocked) */}
            {canReExtract && (
              <button
                type="button"
                onClick={handleReExtract}
                disabled={reExtracting}
                title="Re-run the annexure extractor and overwrite this draft"
                className="inline-flex items-center gap-1.5 text-sm font-medium px-3 py-1 rounded-lg border border-amber-200 dark:border-amber-500/20 bg-amber-50 dark:bg-amber-500/15 hover:bg-amber-100 dark:bg-amber-500/20 text-amber-800 dark:text-amber-400 disabled:opacity-60"
              >
                {reExtracting
                  ? <><Loader2 size={14} className="animate-spin" /> Re-extracting…</>
                  : <><FileSearch size={14} /> Re-extract</>}
              </button>
            )}

            {/* PDF preview pane toggle */}
            <button
              type="button"
              onClick={() => setPreviewPanelOpen((v) => !v)}
              title={previewPanelOpen ? 'Hide PDF preview' : 'Show PDF preview'}
              className="inline-flex items-center gap-1.5 text-sm font-medium px-3 py-1 rounded-lg border border-border bg-card hover:bg-muted/40 text-foreground"
            >
              <Eye size={14} />
              {previewPanelOpen ? 'Hide preview' : 'Show preview'}
            </button>

            {/* Status badge */}
            <span className={`inline-flex items-center gap-1.5 text-sm font-medium px-3 py-1 rounded-full ${status.bg} ${status.color}`}>
              <StatusIcon size={14} />
              {status.label}
            </span>

            {/* Save indicator with transition */}
            <span className={`flex items-center gap-1 text-xs transition-opacity duration-300 ${
              saveStatus === 'idle' ? 'opacity-0' : 'opacity-100'
            } ${saveStatus === 'saved' ? 'text-emerald-500' : 'text-muted-foreground'}`}>
              {saveStatus === 'saving' && (
                <><Loader2 size={12} className="animate-spin" /> Saving...</>
              )}
              {saveStatus === 'saved' && (
                <><CheckCircle2 size={12} /> Saved</>
              )}
            </span>
          </div>
        </div>

        {error && (
          <div className="mb-4 p-3 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg text-sm text-red-700 dark:text-red-400 flex items-center justify-between">
            <span>{error}</span>
            <button
              onClick={() => { setError(''); fetchData(); }}
              className="flex items-center gap-1.5 text-xs font-medium text-red-600 dark:text-red-400 hover:text-red-800 dark:text-red-400 bg-red-100 dark:bg-red-500/20 hover:bg-red-200 px-3 py-1 rounded-lg transition-colors"
            >
              <RefreshCw size={12} />
              Retry
            </button>
          </div>
        )}

        {/* Document info bar with expandable tabs */}
        <DocumentInfoBar
          tenderId={tenderId}
          itemId={checklistItemId}
          item={item}
          workspace={workspace}
          formatTemplate={format_template}
          allItems={allItems}
          onTemplateChange={handleTemplateChange}
          onDependenciesChange={handleDependenciesChange}
          onNotesChange={handleNotesChange}
          onClearLegacySignatures={() => handleSignaturesChange([])}
          onLetterheadChange={handleLetterheadChange}
          onOrientationChange={handleOrientationChange}
          onAgentAssign={handleAgentAssign}
        />

        {/* Split pane: Editor + Agent Chat placeholder */}
        <div className="flex gap-4">
          {/* Left: Editor */}
          <div className="flex-1 min-w-0">
            <div className="bg-card border rounded-xl overflow-hidden">
              <RichTextEditor
                value={editorContent}
                onChange={handleContentChange}
                placeholder="Start writing the document content, or use the AI agent to generate it..."
                minHeight={520}
                signatures={signatures}
                onResolveSignature={async (sigId, kind) => {
                  const r = await getSignatureImageDataUri(sigId, kind);
                  return {
                    dataUri: r.data_uri,
                    name: r.name,
                    designation: r.designation,
                  };
                }}
                onSignatureInserted={async () => {
                  // Flush the auto-save debounce immediately so the data
                  // URI lands in draft_content_html before the preview
                  // re-renders. Then bump previewVersion to trigger an
                  // auto-refresh of the right-side PDF preview pane.
                  await doSave();
                  setPreviewVersion((v) => v + 1);
                }}
              />
            </div>

            {/* Action buttons */}
            <div className="flex items-center gap-3 mt-4">
              <button
                onClick={handleSave}
                disabled={saving || !hasUnsavedChanges}
                className="flex items-center gap-2 bg-indigo-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700 transition-colors disabled:opacity-50"
              >
                <Save size={16} />
                {saving ? 'Saving...' : 'Save Draft'}
              </button>

              <button
                onClick={handleGenerate}
                disabled={generating}
                className="flex items-center gap-2 bg-purple-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-purple-700 transition-colors disabled:opacity-50"
              >
                {generating ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Wand2 size={16} />
                )}
                {generating ? 'Generating...' : 'AI Generate'}
              </button>

              <button
                onClick={() => setShowEnhanceModal(true)}
                disabled={enhancing || (!editorContent && !workspace.draft_content_html)}
                className="flex items-center gap-2 bg-amber-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-amber-700 transition-colors disabled:opacity-50"
              >
                {enhancing ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Sparkles size={16} />
                )}
                {enhancing ? 'Enhancing...' : 'AI Enhance'}
              </button>

              <button
                onClick={handleFinalize}
                disabled={finalizing || (!editorContent && !workspace.draft_content_html)}
                className="flex items-center gap-2 bg-emerald-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-50"
              >
                {finalizing ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <CheckCircle2 size={16} />
                )}
                {finalizing ? 'Finalizing...' : 'Finalize & Generate PDF'}
              </button>
            </div>

            {/* Finalize success banner — shown after a successful PDF generation
                so the user doesn't have to bounce to the Documents tab to know
                it worked. */}
            {finalizedDocId !== null && (
              <div className="mt-3 p-3 bg-emerald-50 dark:bg-emerald-500/15 border border-emerald-200 dark:border-emerald-500/20 rounded-lg text-sm text-emerald-800 dark:text-emerald-400 flex items-start gap-2">
                <CheckCircle2 size={16} className="flex-shrink-0 mt-0.5" />
                <div className="flex-1">
                  <p className="font-medium">PDF generated successfully.</p>
                  <p className="text-xs text-emerald-700 dark:text-emerald-400 mt-0.5">
                    The finalized document
                    {finalizedDocId > 0 ? ` (#${finalizedDocId})` : ''} is now
                    attached to this annexure. Re-finalize any time after edits.
                  </p>
                </div>
                <button
                  onClick={() => setFinalizedDocId(null)}
                  className="text-emerald-600 dark:text-emerald-400 hover:text-emerald-800 dark:text-emerald-400 text-xs"
                  aria-label="Dismiss"
                >
                  ✕
                </button>
              </div>
            )}
          </div>

          {/* Right: PDF preview pane (auto-refreshes when letterhead /
              signature / orientation change; manual refresh button covers
              content edits). Hidden when the user toggles it off to give the
              editor full width. */}
          {previewPanelOpen && (
            <div className="w-96 flex-shrink-0">
              <div className="h-[620px]">
                <DocumentPdfPreview
                  tenderId={tenderId}
                  itemId={checklistItemId}
                  refreshKey={previewVersion}
                  beforeFetch={async () => {
                    if (hasUnsavedChanges) await doSave();
                  }}
                />
              </div>
            </div>
          )}
        </div>

        {/* Enhance modal */}
        {showEnhanceModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="bg-card rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
              <h3 className="text-lg font-semibold text-foreground mb-3">Enhance Document</h3>
              <p className="text-sm text-muted-foreground mb-4">
                Describe how you want the AI to improve the current document.
              </p>
              <textarea
                value={enhancePrompt}
                onChange={(e) => setEnhancePrompt(e.target.value)}
                placeholder="e.g., Add more technical detail to the methodology section, fix formatting issues, add compliance references..."
                rows={4}
                className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent resize-none"
                autoFocus
              />
              <div className="flex justify-end gap-3 mt-4">
                <button
                  onClick={() => { setShowEnhanceModal(false); setEnhancePrompt(''); }}
                  className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleEnhance}
                  disabled={!enhancePrompt.trim()}
                  className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700 transition-colors disabled:opacity-50"
                >
                  Enhance
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Notes section */}
        {item.ai_instructions && (
          <div className="mt-4 bg-amber-50 dark:bg-amber-500/15 border border-amber-200 dark:border-amber-500/20 rounded-xl p-4">
            <h3 className="text-sm font-semibold text-amber-800 dark:text-amber-400 mb-1">AI Instructions</h3>
            <p className="text-sm text-amber-700 dark:text-amber-400">{item.ai_instructions}</p>
          </div>
        )}

        {workspace.notes && (
          <div className="mt-4 bg-muted/40 border rounded-xl p-4">
            <h3 className="text-sm font-semibold text-foreground mb-1">Notes</h3>
            <p className="text-sm text-muted-foreground">{workspace.notes}</p>
          </div>
        )}
      </main>
    </div>
  );
}
