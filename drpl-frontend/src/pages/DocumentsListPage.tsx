import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Plus, Search, FileText, Mail, Award, DollarSign, Trash2, Pencil,
  CheckSquare, Square, X, Download, File,
} from 'lucide-react';
import { isToday, isYesterday, format, parseISO } from 'date-fns';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { getDocuments, deleteDocument, bulkDeleteDocuments } from '../lib/api';
import { formatTimeAgo } from '../lib/formatters';

// ── Types ──────────────────────────────────────────────────────────────────

interface DocumentItem {
  id: number;
  title: string;
  document_type: string;
  tender_id: number | null;
  status: string;
  letterhead_template_id: number | null;
  has_file: boolean;
  file_size: number | null;
  created_at: string;
  updated_at: string;
}

interface DateGroup {
  label: string;
  key: string;
  docs: DocumentItem[];
}

// ── Constants ──────────────────────────────────────────────────────────────

const TYPE_CONFIG: Record<string, { label: string; color: string; icon: any }> = {
  custom: { label: 'Custom', color: 'bg-muted text-muted-foreground', icon: FileText },
  proposal: { label: 'Proposal', color: 'bg-accent/15 text-accent', icon: FileText },
  cost_statement: { label: 'Cost Statement', color: 'bg-amber-100 text-amber-700', icon: DollarSign },
  letter: { label: 'Letter', color: 'bg-emerald-100 text-emerald-700', icon: Mail },
  certificate: { label: 'Certificate', color: 'bg-purple-100 text-purple-700', icon: Award },
};

const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
  draft: { label: 'Draft', color: 'bg-muted text-muted-foreground' },
  generated: { label: 'Generated', color: 'bg-green-100 text-green-700' },
  signed: { label: 'Signed', color: 'bg-accent/15 text-accent' },
  finalized: { label: 'Finalized', color: 'bg-purple-100 text-purple-700' },
};

// ── Confirm Dialog ─────────────────────────────────────────────────────────

function ConfirmDialog({ message, onConfirm, onCancel }: { message: string; onConfirm: () => void; onCancel: () => void }) {
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-card rounded-xl p-6 w-96 shadow-xl">
        <p className="text-sm text-foreground mb-4">{message}</p>
        <div className="flex justify-end gap-2">
          <button onClick={onCancel} className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-muted/40">Cancel</button>
          <button onClick={onConfirm} className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700">Delete</button>
        </div>
      </div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function groupByDate(docs: DocumentItem[]): DateGroup[] {
  const groups = new Map<string, { label: string; docs: DocumentItem[] }>();

  for (const doc of docs) {
    const parsed = parseISO(doc.created_at);
    let key: string;
    let label: string;

    if (isToday(parsed)) {
      key = 'today';
      label = 'Today';
    } else if (isYesterday(parsed)) {
      key = 'yesterday';
      label = 'Yesterday';
    } else {
      key = format(parsed, 'yyyy-MM-dd');
      label = format(parsed, 'dd MMMM yyyy');
    }

    if (!groups.has(key)) {
      groups.set(key, { label, docs: [] });
    }
    groups.get(key)!.docs.push(doc);
  }

  return Array.from(groups.entries()).map(([key, g]) => ({
    key,
    label: g.label,
    docs: g.docs,
  }));
}

function formatFileSize(bytes: number | null): string {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function DocumentsListPage() {
  const navigate = useNavigate();

  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Filters
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');

  // Selection
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  // Delete
  const [deleteTargetId, setDeleteTargetId] = useState<number | null>(null);
  const [showBulkDeleteConfirm, setShowBulkDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // ── Fetch ──────────────────────────────────────────────────────────────

  const fetchDocuments = async () => {
    try {
      setLoading(true);
      const data = await getDocuments();
      setDocuments(data);
    } catch {
      setError('Failed to load documents');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchDocuments(); }, []);

  // ── Filtered & grouped ────────────────────────────────────────────────

  const filtered = useMemo(() => {
    return documents.filter((d) => {
      if (search && !d.title.toLowerCase().includes(search.toLowerCase())) return false;
      if (typeFilter !== 'all' && d.document_type !== typeFilter) return false;
      if (statusFilter !== 'all' && d.status !== statusFilter) return false;
      return true;
    });
  }, [documents, search, typeFilter, statusFilter]);

  const dateGroups = useMemo(() => groupByDate(filtered), [filtered]);

  // ── Selection handlers ────────────────────────────────────────────────

  const toggleSelectMode = () => {
    setSelectMode((prev) => !prev);
    setSelectedIds(new Set());
  };

  const toggleDocSelection = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    setSelectedIds(new Set(filtered.map((d) => d.id)));
  };

  const deselectAll = () => {
    setSelectedIds(new Set());
  };

  // ── Delete handlers ───────────────────────────────────────────────────

  const handleSingleDelete = async () => {
    if (!deleteTargetId) return;
    try {
      setDeleting(true);
      await deleteDocument(deleteTargetId);
      setDocuments((prev) => prev.filter((d) => d.id !== deleteTargetId));
    } catch {
      setError('Failed to delete document');
    } finally {
      setDeleting(false);
      setDeleteTargetId(null);
    }
  };

  const handleBulkDelete = async () => {
    if (selectedIds.size === 0) return;
    try {
      setDeleting(true);
      await bulkDeleteDocuments(Array.from(selectedIds));
      setDocuments((prev) => prev.filter((d) => !selectedIds.has(d.id)));
      setSelectedIds(new Set());
      setSelectMode(false);
    } catch {
      setError('Failed to delete documents');
    } finally {
      setDeleting(false);
      setShowBulkDeleteConfirm(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center h-96">
        <LoadingSpinner />
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-auto bg-background">
      <Header title="Generated documents" subtitle="Review and manage documents prepared in DRPL" />

      <div className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6 lg:py-8">
        {/* ── Error ─────────────────────────────────────────────────── */}
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400 rounded-lg px-4 py-3 text-sm flex items-center justify-between">
            {error}
            <button onClick={() => setError('')} className="text-red-500 hover:text-red-700"><X size={16} /></button>
          </div>
        )}

        {/* ── Top bar ───────────────────────────────────────────────── */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Search */}
          <div className="relative flex-1 min-w-[200px]">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search documents..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-9 pr-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-ring bg-card"
            />
          </div>

          {/* Type filter */}
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="px-3 py-2 text-sm border border-border rounded-lg bg-card focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="all">All Types</option>
            <option value="custom">Custom</option>
            <option value="proposal">Proposal</option>
            <option value="cost_statement">Cost Statement</option>
            <option value="letter">Letter</option>
            <option value="certificate">Certificate</option>
          </select>

          {/* Status filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-3 py-2 text-sm border border-border rounded-lg bg-card focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="all">All Statuses</option>
            <option value="draft">Draft</option>
            <option value="generated">Generated</option>
            <option value="signed">Signed</option>
            <option value="finalized">Finalized</option>
          </select>

          {/* Select toggle */}
          <button
            onClick={toggleSelectMode}
            className={`px-3 py-2 text-sm rounded-lg border transition ${
              selectMode
                ? 'bg-accent/10 border-accent/40 text-accent'
                : 'border-border text-muted-foreground hover:bg-muted/40'
            }`}
          >
            <CheckSquare size={16} className="inline mr-1" />
            {selectMode ? 'Cancel' : 'Select'}
          </button>

          {/* New Document */}
          <button
            onClick={() => navigate('/documents/generate')}
            className="px-4 py-2 text-sm bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 flex items-center gap-1.5"
          >
            <Plus size={16} /> New Document
          </button>
        </div>

        {/* ── Bulk action bar ───────────────────────────────────────── */}
        {selectMode && selectedIds.size > 0 && (
          <div className="flex items-center gap-3 bg-accent/10 border border-accent/20 rounded-lg px-4 py-2.5">
            <span className="text-sm font-medium text-accent">
              {selectedIds.size} selected
            </span>
            <div className="h-4 w-px bg-accent/30" />
            <button onClick={selectAll} className="text-sm text-accent hover:text-accent/80">
              Select All ({filtered.length})
            </button>
            <button onClick={deselectAll} className="text-sm text-accent hover:text-accent/80">
              Deselect All
            </button>
            <div className="flex-1" />
            <button
              onClick={() => setShowBulkDeleteConfirm(true)}
              className="px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 flex items-center gap-1.5"
              disabled={deleting}
            >
              <Trash2 size={14} /> Delete Selected
            </button>
          </div>
        )}

        {/* ── Empty state ───────────────────────────────────────────── */}
        {filtered.length === 0 && !loading && (
          <div className="bg-card rounded-xl border border-border p-12 text-center">
            <div className="w-16 h-16 bg-muted rounded-full flex items-center justify-center mx-auto mb-4">
              <FileText size={28} className="text-muted-foreground" />
            </div>
            <h3 className="text-lg font-semibold text-foreground mb-2">
              {documents.length === 0 ? 'No documents yet' : 'No matching documents'}
            </h3>
            <p className="text-sm text-muted-foreground mb-6">
              {documents.length === 0
                ? 'Create your first document to get started.'
                : 'Try adjusting your search or filters.'}
            </p>
            {documents.length === 0 && (
              <button
                onClick={() => navigate('/documents/generate')}
                className="px-4 py-2 text-sm bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 inline-flex items-center gap-1.5"
              >
                <Plus size={16} /> Create Document
              </button>
            )}
          </div>
        )}

        {/* ── Date-grouped document list ────────────────────────────── */}
        {dateGroups.map((group) => (
          <div key={group.key}>
            {/* Date header */}
            <div className="flex items-center gap-3 mb-2 mt-2">
              <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                {group.label}
              </h3>
              <div className="flex-1 h-px bg-muted" />
              <span className="text-xs text-muted-foreground">{group.docs.length} document{group.docs.length !== 1 ? 's' : ''}</span>
            </div>

            {/* Document rows */}
            <div className="bg-card rounded-xl border border-border divide-y divide-border overflow-hidden">
              {group.docs.map((doc) => {
                const typeConf = TYPE_CONFIG[doc.document_type] || TYPE_CONFIG.custom;
                const statusConf = STATUS_CONFIG[doc.status] || STATUS_CONFIG.draft;
                const TypeIcon = typeConf.icon;
                const isSelected = selectedIds.has(doc.id);

                return (
                  <div
                    key={doc.id}
                    className={`flex items-center gap-3 px-4 py-3 hover:bg-muted/40 transition group ${
                      isSelected ? 'bg-accent/5' : ''
                    }`}
                  >
                    {/* Checkbox */}
                    {selectMode && (
                      <button onClick={() => toggleDocSelection(doc.id)} className="flex-shrink-0">
                        {isSelected
                          ? <CheckSquare size={18} className="text-accent" />
                          : <Square size={18} className="text-muted-foreground/50 hover:text-muted-foreground" />}
                      </button>
                    )}

                    {/* Type icon */}
                    <div className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 ${typeConf.color}`}>
                      <TypeIcon size={16} />
                    </div>

                    {/* Title & meta */}
                    <div className="flex-1 min-w-0">
                      <button
                        onClick={() => navigate(`/documents/library/${doc.id}`)}
                        className="text-sm font-medium text-foreground hover:text-accent truncate block text-left w-full"
                      >
                        {doc.title || 'Untitled Document'}
                      </button>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className={`text-[11px] px-1.5 py-0.5 rounded-full font-medium ${typeConf.color}`}>
                          {typeConf.label}
                        </span>
                        <span className={`text-[11px] px-1.5 py-0.5 rounded-full font-medium ${statusConf.color}`}>
                          {statusConf.label}
                        </span>
                        {doc.has_file && (
                          <span className="text-[11px] text-muted-foreground flex items-center gap-0.5">
                            <File size={10} /> {formatFileSize(doc.file_size)}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Time ago */}
                    <span className="text-xs text-muted-foreground flex-shrink-0 hidden sm:block">
                      {formatTimeAgo(doc.updated_at)}
                    </span>

                    {/* Actions */}
                    <div className="flex items-center gap-1 flex-shrink-0 opacity-0 group-hover:opacity-100 transition">
                      <button
                        onClick={() => navigate(`/documents/library/${doc.id}`)}
                        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-accent"
                        title="Edit"
                      >
                        <Pencil size={15} />
                      </button>
                      <button
                        onClick={() => setDeleteTargetId(doc.id)}
                        className="p-1.5 rounded-md hover:bg-red-500/10 text-muted-foreground hover:text-red-500"
                        title="Delete"
                      >
                        <Trash2 size={15} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {/* ── Delete confirmation dialogs ──────────────────────────────── */}
      {deleteTargetId !== null && (
        <ConfirmDialog
          message="Are you sure you want to delete this document? This action cannot be undone."
          onConfirm={handleSingleDelete}
          onCancel={() => setDeleteTargetId(null)}
        />
      )}

      {showBulkDeleteConfirm && (
        <ConfirmDialog
          message={`Are you sure you want to delete ${selectedIds.size} document${selectedIds.size !== 1 ? 's' : ''}? This action cannot be undone.`}
          onConfirm={handleBulkDelete}
          onCancel={() => setShowBulkDeleteConfirm(false)}
        />
      )}
    </div>
  );
}
