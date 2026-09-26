import { useState, useEffect, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Upload, Search, FileText, Trash2, Pencil, Download, File,
  X, FilePlus2, Loader2,
} from 'lucide-react';
import { isToday, isYesterday, format, parseISO } from 'date-fns';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import {
  getOfflineDocuments, uploadOfflineDocument, deleteOfflineDocument,
  downloadOfflineDocument, type OfflineDocument,
} from '../lib/api';
import { formatTimeAgo } from '../lib/formatters';

// Status chips for offline documents: a doc is either an unsigned draft or a
// signed output.
const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
  draft: { label: 'Draft', color: 'bg-muted text-muted-foreground' },
  signed: { label: 'Signed', color: 'bg-accent/15 text-accent' },
};

interface DateGroup { label: string; key: string; docs: OfflineDocument[] }

function groupByDate(docs: OfflineDocument[]): DateGroup[] {
  const groups = new Map<string, { label: string; docs: OfflineDocument[] }>();
  for (const doc of docs) {
    const parsed = doc.created_at ? parseISO(doc.created_at) : new Date();
    let key: string, label: string;
    if (isToday(parsed)) { key = 'today'; label = 'Today'; }
    else if (isYesterday(parsed)) { key = 'yesterday'; label = 'Yesterday'; }
    else { key = format(parsed, 'yyyy-MM-dd'); label = format(parsed, 'dd MMMM yyyy'); }
    if (!groups.has(key)) groups.set(key, { label, docs: [] });
    groups.get(key)!.docs.push(doc);
  }
  return Array.from(groups.entries()).map(([key, g]) => ({ key, label: g.label, docs: g.docs }));
}

function formatFileSize(bytes: number | null): string {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

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

export default function OfflineDocumentsListPage() {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [documents, setDocuments] = useState<OfflineDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [deleteTargetId, setDeleteTargetId] = useState<number | null>(null);

  const fetchDocuments = async () => {
    try {
      setLoading(true);
      setDocuments(await getOfflineDocuments());
    } catch {
      setError('Failed to load documents');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchDocuments(); }, []);

  const handleUpload = async (file: File) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setError('Only PDF files are supported');
      return;
    }
    setUploading(true);
    setError('');
    try {
      const doc = await uploadOfflineDocument(file);
      navigate(`/documents/sign/${doc.id}`);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Upload failed');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDownload = async (doc: OfflineDocument) => {
    try {
      const blob = await downloadOfflineDocument(doc.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = doc.has_signed_output ? `signed_${doc.title}` : doc.title;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Download failed');
    }
  };

  const handleDelete = async () => {
    if (deleteTargetId == null) return;
    try {
      await deleteOfflineDocument(deleteTargetId);
      setDocuments((prev) => prev.filter((d) => d.id !== deleteTargetId));
    } catch {
      setError('Failed to delete document');
    } finally {
      setDeleteTargetId(null);
    }
  };

  const filtered = useMemo(
    () => documents.filter((d) => !search || d.title.toLowerCase().includes(search.toLowerCase())),
    [documents, search],
  );
  const dateGroups = useMemo(() => groupByDate(filtered), [filtered]);

  if (loading) {
    return <div className="flex-1 flex items-center justify-center h-96"><LoadingSpinner /></div>;
  }

  return (
    <div className="flex-1 overflow-auto bg-background">
      <Header title="My Work" subtitle="Documents that need your attention, review, or signature" />

      <input
        ref={fileInputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) handleUpload(f); }}
      />

      <div className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6 lg:py-8">
        <div>
          <p className="text-xs font-extrabold uppercase tracking-[0.14em] text-emerald-700 dark:text-emerald-400">Document desk</p>
          <h1 className="mt-1 text-2xl font-extrabold tracking-tight">Continue your work</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">Upload a PDF to sign, prepare a new document, or return to something already in progress.</p>
        </div>
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400 rounded-lg px-4 py-3 text-sm flex items-center justify-between">
            {error}
            <button onClick={() => setError('')} className="text-red-500 hover:text-red-700"><X size={16} /></button>
          </div>
        )}

        {/* Top bar */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[200px]">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search documents..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-xl border border-border bg-card py-2.5 pl-9 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {/* Secondary: the old letterhead/TipTap generator */}
          <button
            onClick={() => navigate('/documents/generate')}
            className="flex min-h-11 items-center gap-1.5 rounded-xl border border-border bg-card px-4 py-2 text-sm font-semibold text-muted-foreground hover:border-accent/40 hover:text-foreground"
          >
            <FilePlus2 size={16} /> Generate document
          </button>

          {/* Primary: upload a PDF to sign */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="flex min-h-11 items-center gap-1.5 rounded-xl bg-emerald-600 px-4 py-2 text-sm font-bold text-white hover:-translate-y-px hover:bg-emerald-700 disabled:opacity-60"
          >
            {uploading ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
            {uploading ? 'Uploading…' : 'Upload PDF'}
          </button>
        </div>

        {/* Empty state — big dropzone */}
        {filtered.length === 0 && (
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="w-full rounded-2xl border-2 border-dashed border-border bg-card p-12 text-center shadow-card hover:border-emerald-500/50 hover:bg-emerald-500/5"
          >
            <div className="w-16 h-16 bg-muted rounded-full flex items-center justify-center mx-auto mb-4">
              <Upload size={28} className="text-muted-foreground" />
            </div>
            <h3 className="text-lg font-semibold text-foreground mb-2">
              {documents.length === 0 ? 'Upload a PDF to sign' : 'No matching documents'}
            </h3>
            <p className="text-sm text-muted-foreground">
              {documents.length === 0
                ? 'Drop in an offline PDF, place your signatures, and download the signed copy.'
                : 'Try a different search.'}
            </p>
          </button>
        )}

        {/* Date-grouped list */}
        {dateGroups.map((group) => (
          <div key={group.key}>
            <div className="flex items-center gap-3 mb-2 mt-2">
              <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">{group.label}</h3>
              <div className="flex-1 h-px bg-muted" />
              <span className="text-xs text-muted-foreground">{group.docs.length} document{group.docs.length !== 1 ? 's' : ''}</span>
            </div>

            <div className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card shadow-card">
              {group.docs.map((doc) => {
                const statusConf = STATUS_CONFIG[doc.status] || STATUS_CONFIG.draft;
                return (
                  <div key={doc.id} className="flex items-center gap-3 px-4 py-3 hover:bg-muted/40 transition group">
                    <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 bg-muted text-muted-foreground">
                      <FileText size={16} />
                    </div>

                    <div className="flex-1 min-w-0">
                      <button
                        onClick={() => navigate(`/documents/sign/${doc.id}`)}
                        className="text-sm font-medium text-foreground hover:text-accent truncate block text-left w-full"
                      >
                        {doc.title || 'Untitled PDF'}
                      </button>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className={`text-[11px] px-1.5 py-0.5 rounded-full font-medium ${statusConf.color}`}>
                          {statusConf.label}
                        </span>
                        {doc.file_size != null && (
                          <span className="text-[11px] text-muted-foreground flex items-center gap-0.5">
                            <File size={10} /> {formatFileSize(doc.file_size)}
                          </span>
                        )}
                      </div>
                    </div>

                    <span className="text-xs text-muted-foreground flex-shrink-0 hidden sm:block">
                      {doc.updated_at ? formatTimeAgo(doc.updated_at) : ''}
                    </span>

                    <div className="flex flex-shrink-0 items-center gap-1 opacity-100 transition sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
                      <button
                        onClick={() => handleDownload(doc)}
                        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-accent"
                        title={doc.has_signed_output ? 'Download signed PDF' : 'Download source PDF'}
                      >
                        <Download size={15} />
                      </button>
                      <button
                        onClick={() => navigate(`/documents/sign/${doc.id}`)}
                        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-accent"
                        title="Open"
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

      {deleteTargetId !== null && (
        <ConfirmDialog
          message="Delete this document? This action cannot be undone."
          onConfirm={handleDelete}
          onCancel={() => setDeleteTargetId(null)}
        />
      )}
    </div>
  );
}
