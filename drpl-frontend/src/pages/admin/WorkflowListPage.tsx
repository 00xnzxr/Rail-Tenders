import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  GitBranch, Plus, Trash2, Copy, Star, MoreHorizontal,
  CheckCircle2, Clock, Archive, Layers, ArrowRight,
} from 'lucide-react';
import { listWorkflows, createWorkflow, deleteWorkflow, cloneWorkflow, updateWorkflowById } from '../../lib/api';
import type { WorkflowListItem } from '../../types/workflow';

const STATUS_CONFIG: Record<string, { bg: string; text: string; dot: string }> = {
  draft: { bg: 'bg-amber-50 dark:bg-amber-500/15 border-amber-200 dark:border-amber-500/20', text: 'text-amber-700 dark:text-amber-400', dot: 'bg-amber-400' },
  published: { bg: 'bg-green-50 dark:bg-green-500/15 border-green-200 dark:border-green-500/20', text: 'text-green-700 dark:text-green-400', dot: 'bg-green-400' },
  archived: { bg: 'bg-muted border-border', text: 'text-muted-foreground', dot: 'bg-muted-foreground' },
};

export default function WorkflowListPage() {
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState<WorkflowListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [menuOpen, setMenuOpen] = useState<number | null>(null);

  const load = async () => {
    try {
      const data = await listWorkflows(statusFilter ? { status: statusFilter } : undefined);
      setWorkflows(data);
    } catch (err) {
      console.error('Failed to load workflows:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [statusFilter]);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const wf = await createWorkflow({ display_name: newName.trim(), description: newDescription.trim() || undefined });
      setShowCreateModal(false);
      setNewName('');
      setNewDescription('');
      navigate(`/admin/workflows/${wf.id}`);
    } catch (err) {
      console.error('Failed to create workflow:', err);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this workflow and all its data?')) return;
    setMenuOpen(null);
    try {
      await deleteWorkflow(id);
      load();
    } catch (err) {
      console.error('Failed to delete workflow:', err);
    }
  };

  const handleClone = async (id: number, name: string) => {
    setMenuOpen(null);
    try {
      const cloned = await cloneWorkflow(id, `${name} (Copy)`);
      navigate(`/admin/workflows/${cloned.id}`);
    } catch (err) {
      console.error('Failed to clone workflow:', err);
    }
  };

  const handleSetDefault = async (id: number) => {
    setMenuOpen(null);
    try {
      await updateWorkflowById(id, { is_default_router: true });
      load();
    } catch (err) {
      console.error('Failed to set default router:', err);
    }
  };

  const filterCounts = {
    all: workflows.length,
    draft: workflows.filter((w) => w.status === 'draft').length,
    published: workflows.filter((w) => w.status === 'published').length,
    archived: workflows.filter((w) => w.status === 'archived').length,
  };

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto w-full max-w-5xl p-4 sm:p-6 lg:p-8">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-bold text-foreground flex items-center gap-2.5">
              <div className="p-2 bg-accent/10 rounded-lg">
                <GitBranch size={22} className="text-accent" />
              </div>
              Workflow Builder
            </h1>
            <p className="text-sm text-muted-foreground mt-1.5 ml-[44px]">
              Design and manage multi-agent workflows with a visual drag-and-drop editor
            </p>
          </div>
          <button
            onClick={() => setShowCreateModal(true)}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 transition-colors font-medium text-sm shadow-sm"
          >
            <Plus size={16} />
            New Workflow
          </button>
        </div>

        {/* Filters */}
        <div className="flex gap-1.5 mb-5">
          {(['', 'draft', 'published', 'archived'] as const).map((s) => {
            const label = s || 'All';
            const count = s ? filterCounts[s as keyof typeof filterCounts] : filterCounts.all;
            return (
              <button
                key={label}
                onClick={() => setStatusFilter(s)}
                className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-medium rounded-full border transition-colors ${
                  statusFilter === s
                    ? 'bg-accent/10 border-accent/40 text-accent'
                    : 'bg-card border-border text-muted-foreground hover:bg-muted/40'
                }`}
              >
                {label.charAt(0).toUpperCase() + label.slice(1)}
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                  statusFilter === s ? 'bg-accent/15 text-accent' : 'bg-muted text-muted-foreground'
                }`}>{count}</span>
              </button>
            );
          })}
        </div>

        {/* Workflow Cards */}
        {loading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="bg-card rounded-xl border border-border p-5 animate-pulse">
                <div className="h-5 w-48 bg-muted rounded mb-2" />
                <div className="h-3 w-80 bg-muted rounded mb-3" />
                <div className="flex gap-4">
                  {[1, 2, 3, 4].map((j) => (
                    <div key={j} className="h-3 w-16 bg-muted rounded" />
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : workflows.length === 0 ? (
          <div className="text-center py-20 bg-card rounded-2xl border border-border">
            <div className="w-16 h-16 bg-muted/40 rounded-2xl flex items-center justify-center mx-auto mb-4">
              <GitBranch size={28} className="text-muted-foreground/50" />
            </div>
            <h3 className="text-lg font-semibold text-foreground mb-1">No workflows yet</h3>
            <p className="text-sm text-muted-foreground mb-6 max-w-md mx-auto">
              Create your first workflow to design custom multi-agent pipelines with visual drag-and-drop
            </p>
            <button
              onClick={() => setShowCreateModal(true)}
              className="inline-flex items-center gap-2 px-5 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 text-sm font-medium shadow-sm"
            >
              <Plus size={14} /> Create Your First Workflow
            </button>
          </div>
        ) : (
          <div className="space-y-2.5">
            {workflows.map((wf) => {
              const style = STATUS_CONFIG[wf.status] || STATUS_CONFIG.draft;
              return (
                <div
                  key={wf.id}
                  onClick={() => navigate(`/admin/workflows/${wf.id}`)}
                  className="group bg-card rounded-xl border border-border p-4 hover:border-accent/40 hover:shadow-md transition-all cursor-pointer"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      {/* Title row */}
                      <div className="flex items-center gap-2.5 mb-1">
                        <h3 className="font-semibold text-foreground truncate text-[15px]">{wf.display_name}</h3>
                        {wf.is_default_router && (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-accent/10 border border-accent/20 text-accent text-[10px] font-semibold rounded-full shrink-0">
                            <Star size={9} className="fill-blue-500" /> Default Router
                          </span>
                        )}
                        <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 border text-[10px] font-semibold rounded-full shrink-0 ${style.bg} ${style.text}`}>
                          <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`} />
                          {wf.status}
                        </span>
                      </div>

                      {/* Description */}
                      {wf.description && (
                        <p className="text-xs text-muted-foreground truncate mb-2 max-w-2xl">{wf.description}</p>
                      )}

                      {/* Metadata */}
                      <div className="flex items-center gap-5 text-[11px] text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <Layers size={10} /> {wf.node_count} nodes
                        </span>
                        <span>{wf.edge_count} connections</span>
                        <span>v{wf.current_version}</span>
                        {wf.execution_count > 0 && (
                          <span className="text-accent">{wf.execution_count} executions</span>
                        )}
                        {wf.updated_at && (
                          <span>Updated {new Date(wf.updated_at).toLocaleDateString()}</span>
                        )}
                      </div>
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-1 ml-4 shrink-0">
                      <div className="relative">
                        <button
                          onClick={(e) => { e.stopPropagation(); setMenuOpen(menuOpen === wf.id ? null : wf.id); }}
                          className="p-1.5 rounded-md text-muted-foreground/50 hover:text-muted-foreground hover:bg-muted transition-colors opacity-0 group-hover:opacity-100"
                        >
                          <MoreHorizontal size={16} />
                        </button>
                        {menuOpen === wf.id && (
                          <>
                            <div className="fixed inset-0 z-10" onClick={(e) => { e.stopPropagation(); setMenuOpen(null); }} />
                            <div className="absolute right-0 top-full mt-1 bg-card rounded-lg shadow-lg border border-border py-1 z-20 w-44">
                              {wf.status === 'published' && !wf.is_default_router && (
                                <button
                                  onClick={(e) => { e.stopPropagation(); handleSetDefault(wf.id); }}
                                  className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-accent/10 hover:text-accent/80"
                                >
                                  <Star size={12} /> Set as Default Router
                                </button>
                              )}
                              <button
                                onClick={(e) => { e.stopPropagation(); handleClone(wf.id, wf.display_name); }}
                                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-muted/40"
                              >
                                <Copy size={12} /> Clone Workflow
                              </button>
                              <hr className="my-1 border-border" />
                              <button
                                onClick={(e) => { e.stopPropagation(); handleDelete(wf.id); }}
                                className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:bg-red-500/15"
                              >
                                <Trash2 size={12} /> Delete
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                      <ArrowRight size={16} className="text-muted-foreground/50 group-hover:text-accent/70 transition-colors ml-1" />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Create Modal */}
        {showCreateModal && (
          <div className="fixed inset-0 bg-black/30 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => setShowCreateModal(false)}>
            <div className="bg-card rounded-2xl shadow-xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
              <h2 className="text-lg font-semibold text-foreground mb-1">Create New Workflow</h2>
              <p className="text-xs text-muted-foreground mb-4">Design a custom multi-agent pipeline</p>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Workflow Name</label>
                  <input
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="e.g., Tender Processing Pipeline"
                    className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:border-accent/40"
                    autoFocus
                    onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Description <span className="text-muted-foreground font-normal">(optional)</span></label>
                  <textarea
                    value={newDescription}
                    onChange={(e) => setNewDescription(e.target.value)}
                    placeholder="Describe what this workflow does..."
                    rows={3}
                    className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:border-accent/40 resize-none"
                  />
                </div>
              </div>
              <div className="flex justify-end gap-2 mt-5">
                <button
                  onClick={() => setShowCreateModal(false)}
                  className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted/40"
                >
                  Cancel
                </button>
                <button
                  onClick={handleCreate}
                  disabled={creating || !newName.trim()}
                  className="px-5 py-2 bg-accent text-accent-foreground rounded-lg text-sm font-medium hover:bg-accent/90 disabled:opacity-50 transition-colors shadow-sm"
                >
                  {creating ? 'Creating...' : 'Create Workflow'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
