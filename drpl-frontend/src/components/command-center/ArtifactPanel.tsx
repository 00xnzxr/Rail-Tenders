import { useState, useRef } from 'react';
import {
  FileText, BarChart3, ClipboardList, Search, Pin, Download,
  X, Check, Edit2, ChevronDown, Sparkles, Layers, ArrowLeft, FileSpreadsheet,
  Maximize2, Minimize2, Loader2,
} from 'lucide-react';
import type { Artifact } from '../../types/command-center';
import AgentOutputRenderer from '../agents/AgentOutputRenderer';
import { saveArtifactFile } from '../../lib/api';
import CostBreakdownEditor from './CostBreakdownEditor';
import { XlsxPreview } from './XlsxPreview';

export const TYPE_CONFIG: Record<string, { icon: any; label: string; color: string; accent: string; bg: string; border: string }> = {
  document: {
    icon: FileText,
    label: 'Document',
    color: 'text-accent bg-accent/10 dark:bg-accent/15',
    accent: 'text-accent',
    bg: 'bg-accent/10 dark:bg-accent/100/15',
    border: 'border-accent/20 dark:border-blue-500/20',
  },
  cost_breakdown: {
    icon: BarChart3,
    label: 'Cost Breakdown',
    color: 'text-amber-500 bg-amber-50 dark:bg-amber-500/15 dark:text-amber-400',
    accent: 'text-amber-600 dark:text-amber-400',
    bg: 'bg-amber-50 dark:bg-amber-500/15',
    border: 'border-amber-200 dark:border-amber-500/20',
  },
  cost_breakdown_xlsx: {
    icon: FileSpreadsheet,
    label: 'Cost Spreadsheet',
    color: 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-500/15 dark:text-emerald-400',
    accent: 'text-emerald-600 dark:text-emerald-400',
    bg: 'bg-emerald-50 dark:bg-emerald-500/15',
    border: 'border-emerald-200 dark:border-emerald-500/20',
  },
  checklist: {
    icon: ClipboardList,
    label: 'Checklist',
    color: 'text-emerald-500 bg-emerald-50 dark:bg-emerald-500/15 dark:text-emerald-400',
    accent: 'text-emerald-600 dark:text-emerald-400',
    bg: 'bg-emerald-50 dark:bg-emerald-500/15',
    border: 'border-emerald-200 dark:border-emerald-500/20',
  },
  analysis: {
    icon: Search,
    label: 'Analysis',
    color: 'text-purple-500 bg-purple-50 dark:bg-purple-500/15 dark:text-purple-400',
    accent: 'text-purple-600 dark:text-purple-400',
    bg: 'bg-purple-50 dark:bg-purple-500/15',
    border: 'border-purple-200 dark:border-purple-500/20',
  },
};

interface ArtifactPanelProps {
  artifacts: Artifact[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  onEdit?: (id: number, content: string) => void;
  onExport?: (id: number) => void;
  onClose: () => void;
  /** Side-panel mode (default). When false, renders fullWidth legacy layout. */
  isOpen?: boolean;
  fullWidth?: boolean;
  /** Whether the side panel is expanded to cover the whole chat area. */
  fullScreen?: boolean;
  /** Toggle the full-screen state. When provided, a maximize/minimize control
   *  is shown in the panel header. */
  onToggleFullScreen?: () => void;
  /** Tender id, used by the cost-breakdown editor to load/save data. */
  tenderId?: number;
  /** Command Center session id, used when regenerating XLSX so the new
   * artifact attaches to the right session. */
  sessionId?: number;
  /** Notified when the user regenerates the XLSX so the parent can refresh
   * its artifact list. */
  onArtifactsChanged?: () => void;
}

export default function ArtifactPanel({
  artifacts,
  selectedId,
  onSelect,
  onEdit,
  onExport,
  onClose,
  isOpen,
  fullWidth,
  fullScreen,
  onToggleFullScreen,
  tenderId,
  sessionId,
  onArtifactsChanged,
}: ArtifactPanelProps) {
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  /** Artifact whose file is being fetched right now, so the button can say so. */
  const [downloadingId, setDownloadingId] = useState<number | null>(null);
  const pickerRef = useRef<HTMLDivElement>(null);

  const selected = artifacts.find((a) => a.id === selectedId) ?? artifacts[0] ?? null;

  const handleStartEdit = () => {
    if (selected) { setEditContent(selected.content); setEditing(true); }
  };
  const handleSaveEdit = () => {
    if (selected && onEdit) { onEdit(selected.id, editContent); setEditing(false); }
  };

  const getOutputType = (artifactType: string): string =>
    ({ analysis: 'document_analysis', checklist: 'checklist', document: 'proposal_document', cost_breakdown: 'cost_breakdown' }[artifactType] ?? 'general');

  /** Is this artifact backed by a downloadable binary file (e.g. .xlsx)? */
  const isBinaryArtifact = (a: Artifact | null | undefined): boolean =>
    !!a && (a.artifact_type === 'cost_breakdown_xlsx' || !!(a as any).file_name);

  /** Trigger a file download, showing the user that it started.
   *
   * The download itself lives in `saveArtifactFile` so this panel and the
   * cost-breakdown editor cannot drift apart again. What belongs here is the
   * in-flight state: without it the button gave no acknowledgement at all
   * between the click and the browser's save dialog, which is what made a
   * short wait read as a broken button and invited a second click that starts
   * the whole thing over.
   */
  const downloadArtifactFile = async (id: number, fallbackName: string) => {
    if (downloadingId !== null) return; // already fetching; a second click only slows it down
    setDownloadingId(id);
    try {
      await saveArtifactFile(id, fallbackName);
    } catch (err) {
      console.error('Artifact download failed:', err);
      window.alert(
        'Could not download the Excel file. Please try again — if it keeps ' +
        'failing, re-run the costing for this tender or contact support.',
      );
    } finally {
      setDownloadingId(null);
    }
  };

  // ─── Full-width layout ────────────────────────────────────────────────────
  if (fullWidth) {
    return (
      <div className="flex h-full bg-card rounded-xl border border-border overflow-hidden">
        {/* Left sidebar */}
        <div className="w-72 flex-shrink-0 border-r border-border flex flex-col">
          <div className="px-4 py-3 border-b border-border flex items-center gap-3">
            <button
              onClick={onClose}
              className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
              title="Back to chat"
            >
              <ArrowLeft size={14} />
              Back to Chat
            </button>
            <span className="text-muted-foreground/50">|</span>
            <h3 className="font-semibold text-foreground text-sm">Results <span className="text-muted-foreground font-normal">({artifacts.length})</span></h3>
          </div>
          <div className="flex-1 overflow-y-auto">
            {artifacts.length === 0 ? (
              <div className="px-4 py-12 text-center text-sm text-muted-foreground">
                No results yet. Documents and calculations will appear here as DRPL prepares them.
              </div>
            ) : (
              <div className="p-2 space-y-1">
                {artifacts.map((artifact) => {
                  const cfg = TYPE_CONFIG[artifact.artifact_type] || TYPE_CONFIG.document;
                  const Icon = cfg.icon;
                  const isSel = artifact.id === selectedId;
                  return (
                    <button
                      key={artifact.id}
                      onClick={() => onSelect(artifact.id)}
                      className={`w-full flex items-center gap-2 px-3 py-2.5 rounded-lg text-left text-sm transition-colors ${
                        isSel ? 'bg-accent/10 border border-accent/20' : 'hover:bg-muted/40 border border-transparent'
                      }`}
                    >
                      <div className={`p-1.5 rounded ${cfg.color}`}><Icon size={14} /></div>
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-foreground truncate">{artifact.title}</p>
                        <p className="text-xs text-muted-foreground">Version {artifact.version}</p>
                      </div>
                      {artifact.is_pinned && <Pin size={12} className="text-amber-400" />}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>
        {/* Viewer */}
        <div className="flex-1 overflow-y-auto">
          {selected ? (
            <div className="p-6">
              <div className="flex items-center gap-2 mb-4">
                <span className="text-base font-semibold text-foreground flex-1">{selected.title}</span>
                {!editing ? (
                  <>
                    {!isBinaryArtifact(selected) && (
                      <button onClick={handleStartEdit} className="p-1.5 rounded hover:bg-muted text-muted-foreground hover:text-muted-foreground" title="Edit"><Edit2 size={14} /></button>
                    )}
                    {isBinaryArtifact(selected) ? (
                      <button onClick={() => downloadArtifactFile(selected.id, `${selected.title}.xlsx`)} disabled={downloadingId === selected.id} className="p-1.5 rounded hover:bg-emerald-500/10 text-emerald-500 hover:text-emerald-400 disabled:opacity-60" title={downloadingId === selected.id ? 'Preparing your file…' : 'Download .xlsx'}>{downloadingId === selected.id ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}</button>
                    ) : (
                      onExport && <button onClick={() => onExport(selected.id)} className="p-1.5 rounded hover:bg-muted text-muted-foreground hover:text-muted-foreground" title="Download"><Download size={14} /></button>
                    )}
                  </>
                ) : (
                  <>
                    <button onClick={handleSaveEdit} className="p-1.5 rounded bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/25" title="Save"><Check size={14} /></button>
                    <button onClick={() => setEditing(false)} className="p-1.5 rounded bg-red-500/15 text-red-600 dark:text-red-400 hover:bg-red-500/25" title="Cancel"><X size={14} /></button>
                  </>
                )}
              </div>
              {editing ? (
                <textarea value={editContent} onChange={(e) => setEditContent(e.target.value)}
                  className="w-full h-[calc(100vh-280px)] p-3 border border-border rounded-lg text-sm font-mono resize-y focus:outline-none focus:ring-2 focus:ring-ring" />
              ) : isBinaryArtifact(selected) ? (
                <XlsxPreview artifact={selected} downloading={downloadingId === selected.id} onDownload={() => downloadArtifactFile(selected.id, `${selected.title}.xlsx`)} />
              ) : selected.artifact_type === 'cost_breakdown' && tenderId ? (
                <CostBreakdownEditor
                  tenderId={tenderId}
                  sessionId={sessionId}
                  onXlsxRegenerated={() => onArtifactsChanged?.()}
                />
              ) : (
                <AgentOutputRenderer
                  content={selected.content}
                  outputType={getOutputType(selected.artifact_type)}
                  structuredData={selected.structured_data}
                />
              )}
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
              <FileText size={48} className="mb-3 text-muted-foreground/40" /><p className="text-sm">Select a result to view it</p>
            </div>
          )}
        </div>
      </div>
    );
  }

  // ─── New side-panel mode ───────────────────────────────────────────────────
  const cfg = selected ? (TYPE_CONFIG[selected.artifact_type] || TYPE_CONFIG.document) : null;
  const Icon = cfg?.icon ?? Layers;

  return (
    <div
      className={`
        flex flex-col bg-card border-l border-border w-full h-full
        transition-opacity duration-300 ease-in-out overflow-hidden
        ${isOpen ? 'opacity-100' : 'opacity-0'}
      `}
    >
      {/* ── Panel Header ── */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border flex-shrink-0 bg-muted/40">
        {/* Artifact picker / title */}
        <div className="relative flex-1 min-w-0" ref={pickerRef}>
          <button
            onClick={() => setPickerOpen((v) => !v)}
            disabled={artifacts.length <= 1}
            aria-label={selected ? `Result: ${selected.title}. Select another` : 'Select result'}
            className="flex items-center gap-2 min-w-0 max-w-full group"
          >
            {cfg && (
              <div className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center bg-accent/10 border border-accent/20">
                <Icon size={14} className="text-accent" />
              </div>
            )}
            <div className="min-w-0 text-left">
              <p className="text-sm font-semibold text-foreground truncate leading-tight">
                {selected?.title ?? 'No artifact selected'}
              </p>
              {selected && (
              <p className="text-xs font-medium text-accent">
                  {cfg?.label} · Version {selected.version}
                </p>
              )}
            </div>
            {artifacts.length > 1 && (
              <ChevronDown size={14} className="flex-shrink-0 text-muted-foreground group-hover:text-foreground transition-colors" />
            )}
          </button>

          {/* Artifact picker popover */}
          {pickerOpen && (
            <div className="absolute top-full left-0 mt-2 w-72 bg-popover rounded-xl border border-border shadow-xl z-30 overflow-hidden">
              <div className="px-3 py-2 border-b border-border flex items-center gap-1.5">
                <Sparkles size={13} className="text-accent" />
                <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">All results</span>
                <span className="ml-auto text-xs text-muted-foreground">{artifacts.length}</span>
              </div>
              <div className="max-h-64 overflow-y-auto p-1.5 space-y-0.5">
                {artifacts.map((a) => {
                  const c = TYPE_CONFIG[a.artifact_type] || TYPE_CONFIG.document;
                  const AIcon = c.icon;
                  return (
                    <button
                      key={a.id}
                      onClick={() => { onSelect(a.id); setPickerOpen(false); }}
                      className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-left text-sm transition-colors ${
                        a.id === selectedId ? 'bg-accent/10 border border-accent/20' : 'hover:bg-muted border border-transparent'
                      }`}
                    >
                      <div className="p-1.5 rounded-lg bg-accent/10 text-accent"><AIcon size={13} /></div>
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-foreground truncate">{a.title}</p>
                        <p className="text-xs text-muted-foreground">Version {a.version}</p>
                      </div>
                      {a.is_pinned && <Pin size={11} className="text-warning flex-shrink-0" />}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-1 flex-shrink-0">
          {selected && !editing && (
            <>
              {!isBinaryArtifact(selected) && (
                <button
                  onClick={handleStartEdit}
                  title="Edit"
                  aria-label="Edit result"
                  className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Edit2 size={15} />
                </button>
              )}
              {isBinaryArtifact(selected) ? (
                <button
                  onClick={() => downloadArtifactFile(selected.id, `${selected.title}.xlsx`)}
                  disabled={downloadingId === selected.id}
                  title={downloadingId === selected.id ? 'Preparing your file…' : 'Download .xlsx'}
                  aria-label="Download Excel file"
                  aria-busy={downloadingId === selected.id}
                  className="p-1.5 rounded-lg hover:bg-success/10 text-success hover:text-success transition-colors disabled:opacity-60"
                >
                  {downloadingId === selected.id
                    ? <Loader2 size={15} className="animate-spin" />
                    : <Download size={15} />}
                </button>
              ) : (
                onExport && (
                  <button
                    onClick={() => onExport(selected.id)}
                    title="Download"
                    aria-label="Download result"
                    className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <Download size={15} />
                  </button>
                )
              )}
            </>
          )}
          {editing && (
            <>
              <button onClick={handleSaveEdit} title="Save changes" aria-label="Save changes" className="p-1.5 rounded-lg bg-success/10 text-success hover:bg-success/20 transition-colors"><Check size={15} /></button>
              <button onClick={() => setEditing(false)} title="Cancel edit" aria-label="Cancel edit" className="p-1.5 rounded-lg bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors"><X size={15} /></button>
            </>
          )}
          {onToggleFullScreen && (
            <button
              onClick={onToggleFullScreen}
              title={fullScreen ? 'Exit full screen' : 'Full screen'}
            aria-label={fullScreen ? 'Exit full screen' : 'Expand result to full screen'}
              className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors hidden md:inline-flex"
            >
              {fullScreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
            </button>
          )}
          <button
            onClick={onClose}
            title="Close panel"
            aria-label="Close result panel"
            className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
          >
            <X size={15} />
          </button>
        </div>
      </div>

      {/* ── Content area ── */}
      <div className="flex-1 overflow-y-auto">
        {selected ? (
          <div className={`p-5 ${fullScreen ? 'max-w-5xl mx-auto w-full' : ''}`}>
            {editing ? (
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                className="w-full h-[calc(100vh-200px)] p-3 border border-border rounded-lg text-sm font-mono resize-none focus:outline-none focus:ring-2 focus:ring-ring"
                autoFocus
              />
            ) : isBinaryArtifact(selected) ? (
              <XlsxPreview
                artifact={selected}
                downloading={downloadingId === selected.id}
                onDownload={() => downloadArtifactFile(selected.id, `${selected.title}.xlsx`)}
              />
            ) : selected.artifact_type === 'cost_breakdown' && tenderId ? (
              <CostBreakdownEditor
                tenderId={tenderId}
                sessionId={sessionId}
                onXlsxRegenerated={() => onArtifactsChanged?.()}
              />
            ) : (
              <AgentOutputRenderer
                content={selected.content}
                outputType={getOutputType(selected.artifact_type)}
                structuredData={selected.structured_data}
              />
            )}
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground/50 p-8">
            <div className="w-16 h-16 rounded-2xl bg-muted flex items-center justify-center">
              <Layers size={28} className="text-muted-foreground/50" />
            </div>
            <div className="text-center">
              <p className="text-sm font-medium text-muted-foreground">No result selected</p>
              <p className="text-xs text-muted-foreground/50 mt-1">Documents and calculations will appear here as DRPL prepares them</p>
            </div>
          </div>
        )}
      </div>

      {/* Click-outside to close picker */}
      {pickerOpen && (
        <div className="fixed inset-0 z-20" onClick={() => setPickerOpen(false)} />
      )}
    </div>
  );
}
