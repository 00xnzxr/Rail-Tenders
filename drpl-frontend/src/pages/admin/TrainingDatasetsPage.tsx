import { useState, useEffect, useRef } from 'react';
import {
  Database, Plus, Trash2, Upload, FileText, Eye, X, Search,
  Tag, Archive, RotateCcw, ChevronDown, ChevronRight, AlertCircle,
  CheckCircle2, Loader2, File as FileIcon, Code, FileJson,
} from 'lucide-react';
import {
  getTrainingDatasets, createTrainingDataset, getTrainingDataset,
  updateTrainingDataset, deleteTrainingDataset,
  uploadTrainingDatasetFiles, deleteTrainingDatasetFile,
  previewTrainingDatasetContext,
} from '../../lib/api';

interface DatasetSummary {
  id: number;
  name: string;
  description: string | null;
  tags: string[];
  status: string;
  file_count: number;
  total_size: number;
  created_at: string | null;
}

interface DatasetFile {
  id: number;
  file_name: string;
  file_type: string;
  file_size: number;
  extraction_status: string;
  extraction_error: string | null;
  uploaded_at: string | null;
}

interface DatasetDetail extends DatasetSummary {
  files: DatasetFile[];
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function FileTypeIcon({ type }: { type: string }) {
  switch (type) {
    case 'json': return <FileJson size={16} className="text-amber-500" />;
    case 'jsonl': return <Code size={16} className="text-purple-500" />;
    case 'md': return <FileText size={16} className="text-accent" />;
    default: return <FileIcon size={16} className="text-muted-foreground" />;
  }
}

export default function TrainingDatasetsPage() {
  // Dataset list
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('active');

  // Expanded dataset detail
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [expandedDetail, setExpandedDetail] = useState<DatasetDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Create modal
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createDescription, setCreateDescription] = useState('');
  const [createTags, setCreateTags] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');

  // Edit modal
  const [editDataset, setEditDataset] = useState<DatasetSummary | null>(null);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editTags, setEditTags] = useState('');
  const [saving, setSaving] = useState(false);

  // File upload
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadingTo, setUploadingTo] = useState<number | null>(null);

  // Preview modal
  const [previewContext, setPreviewContext] = useState<string | null>(null);
  const [previewMeta, setPreviewMeta] = useState<{ chars: number; tokens: number } | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<{ type: 'dataset' | 'file'; id: number; name: string } | null>(null);

  useEffect(() => {
    loadDatasets();
  }, [statusFilter]);

  async function loadDatasets() {
    setLoading(true);
    try {
      const data = await getTrainingDatasets(statusFilter || undefined);
      setDatasets(data);
    } catch (e) {
      console.error('Failed to load datasets:', e);
    } finally {
      setLoading(false);
    }
  }

  async function handleExpand(id: number) {
    if (expandedId === id) {
      setExpandedId(null);
      setExpandedDetail(null);
      return;
    }
    setExpandedId(id);
    setLoadingDetail(true);
    try {
      const detail = await getTrainingDataset(id);
      setExpandedDetail(detail);
    } catch (e) {
      console.error('Failed to load dataset detail:', e);
    } finally {
      setLoadingDetail(false);
    }
  }

  async function handleCreate() {
    if (!createName.trim()) return;
    setCreating(true);
    setCreateError('');
    try {
      await createTrainingDataset({
        name: createName.trim(),
        description: createDescription.trim() || undefined,
        tags: createTags.split(',').map(t => t.trim()).filter(Boolean),
      });
      setShowCreateModal(false);
      setCreateName('');
      setCreateDescription('');
      setCreateTags('');
      loadDatasets();
    } catch (e: any) {
      setCreateError(e.response?.data?.detail || 'Failed to create dataset');
    } finally {
      setCreating(false);
    }
  }

  async function handleUpdate() {
    if (!editDataset) return;
    setSaving(true);
    try {
      await updateTrainingDataset(editDataset.id, {
        name: editName.trim(),
        description: editDescription.trim() || null,
        tags: editTags.split(',').map(t => t.trim()).filter(Boolean),
      });
      setEditDataset(null);
      loadDatasets();
      if (expandedId === editDataset.id) {
        const detail = await getTrainingDataset(editDataset.id);
        setExpandedDetail(detail);
      }
    } catch (e) {
      console.error('Failed to update dataset:', e);
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleStatus(ds: DatasetSummary) {
    const newStatus = ds.status === 'active' ? 'archived' : 'active';
    try {
      await updateTrainingDataset(ds.id, { status: newStatus });
      loadDatasets();
    } catch (e) {
      console.error('Failed to update status:', e);
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return;
    try {
      if (deleteTarget.type === 'dataset') {
        await deleteTrainingDataset(deleteTarget.id);
        if (expandedId === deleteTarget.id) {
          setExpandedId(null);
          setExpandedDetail(null);
        }
        loadDatasets();
      } else {
        await deleteTrainingDatasetFile(deleteTarget.id);
        if (expandedId) {
          const detail = await getTrainingDataset(expandedId);
          setExpandedDetail(detail);
        }
        loadDatasets();
      }
    } catch (e) {
      console.error('Failed to delete:', e);
    } finally {
      setDeleteTarget(null);
    }
  }

  async function handleFileUpload(datasetId: number, fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    setUploadingTo(datasetId);
    try {
      const files = Array.from(fileList);
      await uploadTrainingDatasetFiles(datasetId, files);
      // Refresh
      const detail = await getTrainingDataset(datasetId);
      setExpandedDetail(detail);
      loadDatasets();
    } catch (e) {
      console.error('Failed to upload files:', e);
    } finally {
      setUploadingTo(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }

  async function handlePreview(datasetId: number) {
    setLoadingPreview(true);
    setPreviewContext(null);
    try {
      const result = await previewTrainingDatasetContext(datasetId);
      setPreviewContext(result.context);
      setPreviewMeta({ chars: result.char_count, tokens: result.estimated_tokens });
    } catch (e: any) {
      setPreviewContext(e.response?.data?.detail || 'Failed to load preview');
    } finally {
      setLoadingPreview(false);
    }
  }

  function openEdit(ds: DatasetSummary) {
    setEditDataset(ds);
    setEditName(ds.name);
    setEditDescription(ds.description || '');
    setEditTags((ds.tags || []).join(', '));
  }

  const filteredDatasets = datasets.filter(ds => {
    if (!searchQuery) return true;
    const q = searchQuery.toLowerCase();
    return ds.name.toLowerCase().includes(q) ||
      (ds.description || '').toLowerCase().includes(q) ||
      (ds.tags || []).some(t => t.toLowerCase().includes(q));
  });

  return (
    <div className="mx-auto w-full max-w-6xl p-4 sm:p-6 lg:p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-foreground flex items-center gap-2">
            <Database size={28} className="text-indigo-600 dark:text-indigo-400" />
            Training Datasets
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Upload and manage knowledge files that agents use as training context
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition text-sm font-medium"
        >
          <Plus size={16} /> New Dataset
        </button>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search datasets..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
          />
        </div>
        <select
          value={statusFilter}
          onChange={e => setStatusFilter(e.target.value)}
          className="px-3 py-2 border border-border rounded-lg text-sm"
        >
          <option value="">All Status</option>
          <option value="active">Active</option>
          <option value="archived">Archived</option>
        </select>
      </div>

      {/* Dataset List */}
      {loading ? (
        <div className="text-center py-12 text-muted-foreground">
          <Loader2 className="animate-spin mx-auto mb-2" size={24} />
          Loading datasets...
        </div>
      ) : filteredDatasets.length === 0 ? (
        <div className="text-center py-16 bg-card rounded-xl border border-border">
          <Database size={48} className="mx-auto text-muted-foreground/50 mb-3" />
          <p className="text-muted-foreground text-sm">
            {searchQuery ? 'No datasets match your search' : 'No training datasets yet'}
          </p>
          {!searchQuery && (
            <button
              onClick={() => setShowCreateModal(true)}
              className="mt-3 text-sm text-indigo-600 dark:text-indigo-400 hover:underline"
            >
              Create your first dataset
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {filteredDatasets.map(ds => (
            <div key={ds.id} className="bg-card rounded-xl border border-border overflow-hidden">
              {/* Dataset Row */}
              <div
                className="flex items-center gap-4 px-5 py-4 cursor-pointer hover:bg-muted/40 transition"
                onClick={() => handleExpand(ds.id)}
              >
                <div className="text-muted-foreground">
                  {expandedId === ds.id ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="font-semibold text-foreground truncate">{ds.name}</h3>
                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                      ds.status === 'active' ? 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400' : 'bg-muted text-muted-foreground'
                    }`}>
                      {ds.status}
                    </span>
                  </div>
                  {ds.description && (
                    <p className="text-sm text-muted-foreground truncate mt-0.5">{ds.description}</p>
                  )}
                  {ds.tags && ds.tags.length > 0 && (
                    <div className="flex items-center gap-1 mt-1.5 flex-wrap">
                      {ds.tags.map((tag, i) => (
                        <span key={i} className="px-2 py-0.5 bg-indigo-50 dark:bg-indigo-500/15 text-indigo-600 dark:text-indigo-400 rounded text-xs">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="text-right text-sm text-muted-foreground shrink-0">
                  <div>{ds.file_count} file{ds.file_count !== 1 ? 's' : ''}</div>
                  <div className="text-xs text-muted-foreground">{formatBytes(ds.total_size)}</div>
                </div>
                <div className="flex items-center gap-1 shrink-0" onClick={e => e.stopPropagation()}>
                  <button
                    onClick={() => openEdit(ds)}
                    className="p-1.5 text-muted-foreground hover:text-indigo-600 dark:text-indigo-400 rounded transition"
                    title="Edit"
                  >
                    <Tag size={15} />
                  </button>
                  <button
                    onClick={() => handleToggleStatus(ds)}
                    className="p-1.5 text-muted-foreground hover:text-amber-600 dark:text-amber-400 rounded transition"
                    title={ds.status === 'active' ? 'Archive' : 'Reactivate'}
                  >
                    {ds.status === 'active' ? <Archive size={15} /> : <RotateCcw size={15} />}
                  </button>
                  <button
                    onClick={() => setDeleteTarget({ type: 'dataset', id: ds.id, name: ds.name })}
                    className="p-1.5 text-muted-foreground hover:text-red-600 dark:text-red-400 rounded transition"
                    title="Delete"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>

              {/* Expanded Detail */}
              {expandedId === ds.id && (
                <div className="border-t border-border bg-muted/40 px-5 py-4">
                  {loadingDetail ? (
                    <div className="text-center py-6 text-muted-foreground">
                      <Loader2 className="animate-spin mx-auto" size={20} />
                    </div>
                  ) : expandedDetail ? (
                    <>
                      {/* Actions Row */}
                      <div className="flex items-center gap-2 mb-4">
                        <label className="flex items-center gap-2 px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition text-sm cursor-pointer">
                          {uploadingTo === ds.id ? (
                            <Loader2 size={14} className="animate-spin" />
                          ) : (
                            <Upload size={14} />
                          )}
                          Upload Files
                          <input
                            ref={fileInputRef}
                            type="file"
                            accept=".md,.json,.jsonl,.txt"
                            multiple
                            className="hidden"
                            onChange={e => handleFileUpload(ds.id, e.target.files)}
                            disabled={uploadingTo === ds.id}
                          />
                        </label>
                        <button
                          onClick={() => handlePreview(ds.id)}
                          className="flex items-center gap-1.5 px-3 py-1.5 border border-border rounded-lg hover:bg-card transition text-sm text-muted-foreground"
                        >
                          <Eye size={14} /> Preview Context
                        </button>
                        <span className="text-xs text-muted-foreground ml-2">
                          Accepts: .md, .json, .jsonl, .txt (max 10 MB)
                        </span>
                      </div>

                      {/* Files List */}
                      {expandedDetail.files.length === 0 ? (
                        <p className="text-sm text-muted-foreground py-4 text-center">
                          No files yet. Upload knowledge files to get started.
                        </p>
                      ) : (
                        <div className="space-y-1.5">
                          {expandedDetail.files.map(f => (
                            <div key={f.id} className="flex items-center gap-3 px-3 py-2 bg-card rounded-lg border border-border">
                              <FileTypeIcon type={f.file_type} />
                              <span className="text-sm font-medium text-foreground flex-1 truncate">
                                {f.file_name}
                              </span>
                              <span className="text-xs text-muted-foreground px-2 py-0.5 bg-muted/40 rounded">
                                {f.file_type.toUpperCase()}
                              </span>
                              <span className="text-xs text-muted-foreground">{formatBytes(f.file_size)}</span>
                              {f.extraction_status === 'completed' ? (
                                <CheckCircle2 size={14} className="text-green-500" />
                              ) : f.extraction_status === 'failed' ? (
                                <span title={f.extraction_error || 'Parse failed'}>
                                  <AlertCircle size={14} className="text-amber-500" />
                                </span>
                              ) : (
                                <Loader2 size={14} className="text-muted-foreground animate-spin" />
                              )}
                              <button
                                onClick={() => setDeleteTarget({ type: 'file', id: f.id, name: f.file_name })}
                                className="p-1 text-muted-foreground hover:text-red-600 dark:text-red-400 transition"
                              >
                                <Trash2 size={14} />
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  ) : null}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Create Dataset Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-card rounded-2xl shadow-2xl w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-foreground">New Training Dataset</h2>
              <button onClick={() => { setShowCreateModal(false); setCreateError(''); }} className="text-muted-foreground hover:text-muted-foreground">
                <X size={20} />
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-muted-foreground mb-1">DATASET NAME *</label>
                <input
                  type="text"
                  value={createName}
                  onChange={e => setCreateName(e.target.value)}
                  placeholder="e.g., Railway Knowledge Base"
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-muted-foreground mb-1">DESCRIPTION</label>
                <textarea
                  value={createDescription}
                  onChange={e => setCreateDescription(e.target.value)}
                  placeholder="What kind of knowledge does this dataset contain?"
                  rows={2}
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-muted-foreground mb-1">TAGS (comma-separated)</label>
                <input
                  type="text"
                  value={createTags}
                  onChange={e => setCreateTags(e.target.value)}
                  placeholder="railway, electrical, power-car"
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              {createError && (
                <p className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15 px-3 py-2 rounded-lg">{createError}</p>
              )}
            </div>
            <div className="flex justify-end gap-2 mt-5">
              <button
                onClick={() => { setShowCreateModal(false); setCreateError(''); }}
                className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={!createName.trim() || creating}
                className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2"
              >
                {creating && <Loader2 size={14} className="animate-spin" />}
                Create Dataset
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit Dataset Modal */}
      {editDataset && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-card rounded-2xl shadow-2xl w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-foreground">Edit Dataset</h2>
              <button onClick={() => setEditDataset(null)} className="text-muted-foreground hover:text-muted-foreground">
                <X size={20} />
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-muted-foreground mb-1">NAME</label>
                <input
                  type="text"
                  value={editName}
                  onChange={e => setEditName(e.target.value)}
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-muted-foreground mb-1">DESCRIPTION</label>
                <textarea
                  value={editDescription}
                  onChange={e => setEditDescription(e.target.value)}
                  rows={2}
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-xs font-semibold text-muted-foreground mb-1">TAGS (comma-separated)</label>
                <input
                  type="text"
                  value={editTags}
                  onChange={e => setEditTags(e.target.value)}
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-indigo-500"
                />
              </div>
            </div>
            <div className="flex justify-end gap-2 mt-5">
              <button onClick={() => setEditDataset(null)} className="px-4 py-2 text-sm text-muted-foreground">Cancel</button>
              <button
                onClick={handleUpdate}
                disabled={!editName.trim() || saving}
                className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2"
              >
                {saving && <Loader2 size={14} className="animate-spin" />}
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-card rounded-2xl shadow-2xl w-full max-w-sm p-6">
            <h2 className="text-lg font-semibold text-foreground mb-2">Confirm Delete</h2>
            <p className="text-sm text-muted-foreground mb-4">
              Are you sure you want to delete{' '}
              <span className="font-medium text-foreground">{deleteTarget.name}</span>?
              {deleteTarget.type === 'dataset' && (
                <span className="block mt-1 text-red-600 dark:text-red-400">
                  This will also delete all files and agent assignments.
                </span>
              )}
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setDeleteTarget(null)} className="px-4 py-2 text-sm text-muted-foreground">
                Cancel
              </button>
              <button
                onClick={handleDelete}
                className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm hover:bg-red-700"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Preview Context Modal */}
      {(previewContext !== null || loadingPreview) && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-card rounded-2xl shadow-2xl w-full max-w-3xl max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-4 border-b border-border">
              <div>
                <h2 className="text-lg font-semibold text-foreground">Context Preview</h2>
                {previewMeta && (
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {previewMeta.chars.toLocaleString()} characters ~ {previewMeta.tokens.toLocaleString()} tokens
                  </p>
                )}
              </div>
              <button
                onClick={() => { setPreviewContext(null); setPreviewMeta(null); }}
                className="text-muted-foreground hover:text-muted-foreground"
              >
                <X size={20} />
              </button>
            </div>
            <div className="flex-1 overflow-auto p-6">
              {loadingPreview ? (
                <div className="text-center py-8 text-muted-foreground">
                  <Loader2 className="animate-spin mx-auto" size={24} />
                </div>
              ) : (
                <pre className="text-sm text-foreground whitespace-pre-wrap font-mono bg-muted/40 rounded-lg p-4 border border-border">
                  {previewContext}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
