import { useState, useEffect, useCallback } from 'react';
import { LayoutGrid, Columns3, List, Network, Loader2, FolderOpen, RefreshCw, Files, ChevronRight } from 'lucide-react';
import WorkspaceProgressBar from '../workspace/WorkspaceProgressBar';
import DocumentCardGrid from '../workspace/DocumentCardGrid';
import WorkspaceKanbanBoard from '../workspace/WorkspaceKanbanBoard';
import WorkspaceCanvasView from '../workspace/WorkspaceCanvasView';
import SkeletonCard from '../workspace/SkeletonCard';
import SkeletonProgressBar from '../workspace/SkeletonProgressBar';
import {
  getWorkspace, initWorkspace,
  updateWorkspaceConfig, toggleDocumentNotRequired,
  getFormatTemplates, updateDocumentWorkspace,
  saveWorkspaceLayout,
} from '../../lib/api';
import type { WorkspaceOverview, WorkspaceItem, WorkspaceStats, WorkspaceConfig, DocumentFormatTemplate, CanvasLayout } from '../../types/workspace';

const VIEW_MODES = [
  { key: 'grid', icon: LayoutGrid, label: 'Grid' },
  { key: 'kanban', icon: Columns3, label: 'Kanban' },
  { key: 'list', icon: List, label: 'List' },
  { key: 'canvas', icon: Network, label: 'Canvas' },
] as const;

interface WorkspaceTabContentProps {
  tenderId: number;
  onOpenDocument: (itemId: number) => void;
  /** Opens the stacked single-tab annexures editor. When omitted, annexures
   *  fall back to the per-document editor. */
  onOpenAnnexures?: (itemId?: number) => void;
  compact?: boolean;
}

const ANNEXURE_PREFIX = 'annexure_finder:';

export default function WorkspaceTabContent({ tenderId, onOpenDocument, onOpenAnnexures, compact }: WorkspaceTabContentProps) {
  const [config, setConfig] = useState<WorkspaceConfig | null>(null);
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [stats, setStats] = useState<WorkspaceStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [initializing, setInitializing] = useState(false);
  const [workspaceExists, setWorkspaceExists] = useState(false);
  const [error, setError] = useState('');
  const [templateMap, setTemplateMap] = useState<Record<number, DocumentFormatTemplate>>({});

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      setError('');

      try {
        const overview: WorkspaceOverview = await getWorkspace(tenderId);
        setConfig(overview.config);
        setItems(overview.items);
        setStats(overview.stats);
        setWorkspaceExists(true);

        // Load format templates for card badges
        try {
          const templates = await getFormatTemplates(tenderId);
          const map: Record<number, DocumentFormatTemplate> = {};
          for (const t of templates) map[t.id] = t;
          setTemplateMap(map);
        } catch {
          // Non-critical
        }
      } catch {
        // Workspace not initialized yet
        setWorkspaceExists(false);
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load workspace data');
    } finally {
      setLoading(false);
    }
  }, [tenderId]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleInit = async () => {
    setInitializing(true);
    setError('');
    try {
      const overview: WorkspaceOverview = await initWorkspace(tenderId);
      setConfig(overview.config);
      setItems(overview.items);
      setStats(overview.stats);
      setWorkspaceExists(true);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to initialize workspace');
    } finally {
      setInitializing(false);
    }
  };

  const handleViewModeChange = async (mode: string) => {
    if (!config) return;
    setConfig({ ...config, view_mode: mode as any });
    try {
      await updateWorkspaceConfig(tenderId, { view_mode: mode });
    } catch { /* best-effort */ }
  };

  const handleToggleNotRequired = async (itemId: number) => {
    try {
      const result = await toggleDocumentNotRequired(tenderId, itemId);
      setItems((prev) =>
        prev.map((item) =>
          item.id === itemId
            ? {
                ...item,
                is_not_required: result.is_not_required,
                workspace_status: result.is_not_required ? 'approved' : 'not_started',
                review_status: result.is_not_required ? 'approved' : 'not_started',
              }
            : item
        )
      );
      const overview: WorkspaceOverview = await getWorkspace(tenderId);
      setStats(overview.stats);
      setConfig(overview.config);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to update');
    }
  };

  const handleStatusChange = async (itemId: number, newStatus: string) => {
    const previousItems = [...items];
    setItems((prev) =>
      prev.map((item) =>
        item.id === itemId
          ? { ...item, review_status: newStatus as any, workspace_status: newStatus }
          : item
      )
    );

    try {
      await updateDocumentWorkspace(tenderId, itemId, { review_status: newStatus });
      const overview: WorkspaceOverview = await getWorkspace(tenderId);
      setStats(overview.stats);
    } catch (err: any) {
      setItems(previousItems);
      setError(err.response?.data?.detail || 'Failed to update status');
    }
  };

  // Annexures now open in the stacked single-tab editor; everything else uses
  // the per-document editor. Falls back to per-document if no handler given.
  const annexureItems = items.filter((it) => (it.source_section || '').startsWith(ANNEXURE_PREFIX));
  const annexureIds = new Set(annexureItems.map((it) => it.id));
  const handleOpen = (itemId: number) => {
    if (annexureIds.has(itemId) && onOpenAnnexures) onOpenAnnexures(itemId);
    else onOpenDocument(itemId);
  };
  const annexuresApproved = annexureItems.filter((it) => it.review_status === 'approved').length;

  const handleLayoutSave = (layout: CanvasLayout) => {
    saveWorkspaceLayout(tenderId, layout).catch(() => {});
    if (config) {
      setConfig({ ...config, layout_json: layout });
    }
  };

  // Skeleton loading state
  if (loading) {
    return (
      <div className={compact ? 'p-4' : ''}>
        {/* Skeleton view mode toggle */}
        <div className="flex items-center justify-end mb-4 animate-pulse">
          <div className="flex items-center gap-1 bg-card border rounded-lg p-1">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="h-7 w-16 bg-muted rounded" />
            ))}
          </div>
        </div>

        <SkeletonProgressBar />

        <div className="mt-6 space-y-8">
          <div>
            <div className="flex items-center gap-2 mb-4 animate-pulse">
              <div className="h-4 w-4 bg-muted rounded" />
              <div className="h-4 w-36 bg-muted rounded" />
              <div className="h-4 w-6 bg-muted rounded-full" />
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {Array.from({ length: 8 }).map((_, i) => (
                <SkeletonCard key={i} />
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={compact ? 'p-4' : ''}>
      {/* View mode toggle */}
      {workspaceExists && (
        <div className="flex items-center justify-end mb-4">
          <div className="flex items-center gap-1 bg-card border rounded-lg p-1">
            {VIEW_MODES.map(({ key, icon: Icon, label }) => (
              <button
                key={key}
                onClick={() => handleViewModeChange(key)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  config?.view_mode === key
                    ? 'bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted/40'
                }`}
                title={label}
              >
                <Icon size={14} />
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

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

      {/* Workspace not initialized */}
      {!workspaceExists && !loading && (
        <div className="text-center py-20">
          <FolderOpen size={48} className="mx-auto text-muted-foreground/50 mb-4" />
          <h2 className="text-xl font-semibold text-foreground mb-2">
            Canvas Workspace
          </h2>
          <p className="text-muted-foreground mb-6 max-w-md mx-auto">
            Initialize a dedicated workspace for this tender where each document
            can be individually edited, reviewed, and finalized with AI assistance.
          </p>
          <button
            onClick={handleInit}
            disabled={initializing}
            className="inline-flex items-center gap-2 bg-indigo-600 text-white px-6 py-3 rounded-lg font-medium hover:bg-indigo-700 transition-colors disabled:opacity-50"
          >
            {initializing ? (
              <>
                <Loader2 size={18} className="animate-spin" />
                Initializing...
              </>
            ) : (
              <>
                <LayoutGrid size={18} />
                Initialize Workspace
              </>
            )}
          </button>
        </div>
      )}

      {/* Workspace content */}
      {workspaceExists && stats && (
        <div className="space-y-6">
          {/* Progress bar */}
          <div className="bg-card border rounded-xl p-4">
            <WorkspaceProgressBar stats={stats} />
          </div>

          {/* Annexures banner — opens the stacked single-tab editor where all
              annexures are filled together and exported in one shot. */}
          {onOpenAnnexures && annexureItems.length > 0 && (
            <button
              type="button"
              onClick={() => onOpenAnnexures()}
              className="w-full flex items-center gap-3 bg-indigo-50 dark:bg-indigo-500/10 border border-indigo-200 dark:border-indigo-500/20 rounded-xl p-4 text-left hover:bg-indigo-100 dark:hover:bg-indigo-500/15 transition-colors"
            >
              <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-indigo-100 dark:bg-indigo-500/20 flex items-center justify-center">
                <Files size={20} className="text-indigo-600 dark:text-indigo-400" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-foreground">
                  All Annexures ({annexureItems.length})
                </p>
                <p className="text-xs text-muted-foreground">
                  {annexuresApproved} of {annexureItems.length} approved · open the stacked editor to fill &amp; export them together
                </p>
              </div>
              <ChevronRight size={18} className="text-indigo-500 flex-shrink-0" />
            </button>
          )}

          {/* Grid view */}
          {config?.view_mode === 'grid' && (
            <DocumentCardGrid
              items={items}
              templateMap={templateMap}
              onOpen={handleOpen}
              onToggleNotRequired={handleToggleNotRequired}
            />
          )}

          {/* Kanban view */}
          {config?.view_mode === 'kanban' && (
            <WorkspaceKanbanBoard
              items={items}
              templateMap={templateMap}
              onStatusChange={handleStatusChange}
              onOpen={handleOpen}
              onToggleNotRequired={handleToggleNotRequired}
            />
          )}

          {/* List view */}
          {config?.view_mode === 'list' && (
            <div className="bg-card border rounded-xl divide-y">
              {items.map((item) => (
                <div
                  key={item.id}
                  onClick={() => handleOpen(item.id)}
                  className={`flex items-center gap-4 px-4 py-3 hover:bg-muted/40 cursor-pointer transition-colors ${
                    item.is_not_required ? 'opacity-50' : ''
                  }`}
                >
                  <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                    item.review_status === 'approved' ? 'bg-emerald-500' :
                    item.review_status === 'drafting' ? 'bg-accent/100' :
                    item.review_status === 'in_review' ? 'bg-amber-500' :
                    'bg-muted-foreground/40'
                  }`} />
                  <span className="flex-1 text-sm font-medium text-foreground truncate">
                    {item.item_name}
                  </span>
                  <span className="text-xs text-muted-foreground capitalize">
                    {item.item_category}
                  </span>
                  <span className={`text-xs font-medium ${
                    item.review_status === 'approved' ? 'text-emerald-600 dark:text-emerald-400' :
                    item.review_status === 'drafting' ? 'text-accent' :
                    'text-muted-foreground'
                  }`}>
                    {item.is_not_required ? 'Not Required' : item.review_status.replace('_', ' ')}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Canvas view */}
          {config?.view_mode === 'canvas' && (
            <WorkspaceCanvasView
              items={items}
              templateMap={templateMap}
              layoutJson={config.layout_json}
              onLayoutSave={handleLayoutSave}
              onOpen={handleOpen}
            />
          )}
        </div>
      )}
    </div>
  );
}
