import { useState, useEffect, useCallback, useRef } from 'react';
import {
  FileText, Download, Upload, RefreshCw, CheckCircle, Clock, Loader2,
  XCircle, Lock, FolderOpen, AlertTriangle,
} from 'lucide-react';
import {
  getSessionDocuments, triggerDocumentDownload, uploadSessionDocument, triggerBulkDownload,
} from '../../lib/api';

interface TenderDoc {
  id: number;
  file_name: string;
  file_size: number;
  mime_type: string;
  document_type: string;
  extraction_status: string;
  gem_file_id: string | null;
  source_url: string | null;
  parent_document_id: number | null;
  uploaded_at: string | null;
}

interface Props {
  sessionId: number;
  tenderId: number;
}

const STATUS_CONFIG: Record<string, { icon: typeof CheckCircle; color: string; label: string }> = {
  completed: { icon: CheckCircle, color: 'text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-500/15', label: 'Available' },
  pending: { icon: Clock, color: 'text-yellow-600 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-500/15', label: 'Queued' },
  processing: { icon: Loader2, color: 'text-accent bg-accent/10', label: 'Downloading...' },
  failed: { icon: XCircle, color: 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15', label: 'Failed' },
  failed_auth: { icon: Lock, color: 'text-orange-600 dark:text-orange-400 bg-orange-50 dark:bg-orange-500/15', label: 'Auth Required' },
  skipped: { icon: AlertTriangle, color: 'text-muted-foreground bg-muted/40', label: 'Skipped' },
};

const TYPE_LABELS: Record<string, string> = {
  gem_referenced: 'GEM Referenced',
  linked_document: 'Linked',
  manual_upload: 'Uploaded',
  checklist_upload: 'Checklist',
  tender_notice: 'NIT',
};

function formatSize(bytes: number): string {
  if (!bytes) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function DocumentsTabContent({ sessionId, tenderId }: Props) {
  const [documents, setDocuments] = useState<TenderDoc[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pendingCount, setPendingCount] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [bulkDownloading, setBulkDownloading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchDocuments = useCallback(async () => {
    try {
      const result = await getSessionDocuments(sessionId);
      setDocuments(result.documents || []);
      setPendingCount(result.pending_count || 0);
      setError('');
    } catch {
      setError('Failed to load documents');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    fetchDocuments();
  }, [fetchDocuments]);

  // Auto-refresh while documents are pending
  useEffect(() => {
    if (pendingCount > 0) {
      pollRef.current = setInterval(fetchDocuments, 10000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [pendingCount, fetchDocuments]);

  const handleRetry = async (docId: number) => {
    try {
      await triggerDocumentDownload(sessionId, docId);
      setDocuments((prev) =>
        prev.map((d) => (d.id === docId ? { ...d, extraction_status: 'pending' } : d))
      );
    } catch {
      // Ignore
    }
  };

  const handleBulkDownload = async () => {
    setBulkDownloading(true);
    try {
      await triggerBulkDownload(sessionId);
      await fetchDocuments();
    } catch {
      // Ignore
    } finally {
      setBulkDownloading(false);
    }
  };

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      await uploadSessionDocument(sessionId, file);
      await fetchDocuments();
    } catch {
      setError('Upload failed');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleUploadForGem = async (doc: TenderDoc) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf,.doc,.docx,.xls,.xlsx,.zip,.csv,.txt';
    input.onchange = async (e: any) => {
      const file = e.target?.files?.[0];
      if (!file) return;
      setUploading(true);
      try {
        await uploadSessionDocument(sessionId, file, doc.gem_file_id || undefined);
        await fetchDocuments();
      } catch {
        setError('Upload failed');
      } finally {
        setUploading(false);
      }
    };
    input.click();
  };

  // Separate documents by status groups
  const available = documents.filter((d) => d.extraction_status === 'completed');
  const pending = documents.filter((d) => ['pending', 'processing'].includes(d.extraction_status));
  const failed = documents.filter((d) => ['failed', 'failed_auth'].includes(d.extraction_status));

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 size={24} className="animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!tenderId) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
        <FolderOpen size={32} className="mb-3" />
        <p className="text-sm">Link a tender to manage documents</p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto px-6 py-4">
      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 rounded-lg text-sm">{error}</div>
      )}

      {/* Actions Bar */}
      <div className="flex items-center gap-3 mb-6">
        <h3 className="text-sm font-semibold text-foreground flex-1">
          Tender Documents ({documents.length})
        </h3>
        {failed.length > 0 && (
          <button
            onClick={handleBulkDownload}
            disabled={bulkDownloading}
            className="flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 bg-accent/10 text-accent hover:bg-accent/15 rounded-lg transition-colors disabled:opacity-50"
          >
            {bulkDownloading ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
            Retry All Downloads
          </button>
        )}
        <button
          onClick={() => fetchDocuments()}
          className="flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 bg-muted text-muted-foreground hover:bg-muted rounded-lg transition-colors"
        >
          <RefreshCw size={12} />
          Refresh
        </button>
        <label className="flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 hover:bg-emerald-100 dark:bg-emerald-500/20 rounded-lg transition-colors cursor-pointer">
          {uploading ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}
          Upload
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            accept=".pdf,.doc,.docx,.xls,.xlsx,.zip,.csv,.txt"
            onChange={handleUpload}
            disabled={uploading}
          />
        </label>
      </div>

      {documents.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
          <FolderOpen size={32} className="mb-3" />
          <p className="text-sm">No documents yet</p>
          <p className="text-xs mt-1">Documents will appear here after tender analysis</p>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Pending Downloads */}
          {pending.length > 0 && (
            <DocumentGroup title="Downloading" count={pending.length} docs={pending} onRetry={handleRetry} onUploadFor={handleUploadForGem} />
          )}

          {/* Failed / Auth Required */}
          {failed.length > 0 && (
            <DocumentGroup title="Needs Attention" count={failed.length} docs={failed} onRetry={handleRetry} onUploadFor={handleUploadForGem} />
          )}

          {/* Available */}
          {available.length > 0 && (
            <DocumentGroup title="Available" count={available.length} docs={available} onRetry={handleRetry} onUploadFor={handleUploadForGem} />
          )}
        </div>
      )}
    </div>
  );
}

function DocumentGroup({
  title, count, docs, onRetry, onUploadFor,
}: {
  title: string;
  count: number;
  docs: TenderDoc[];
  onRetry: (id: number) => void;
  onUploadFor: (doc: TenderDoc) => void;
}) {
  return (
    <div>
      <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
        {title} ({count})
      </h4>
      <div className="space-y-1.5">
        {docs.map((doc) => (
          <DocumentRow key={doc.id} doc={doc} onRetry={onRetry} onUploadFor={onUploadFor} />
        ))}
      </div>
    </div>
  );
}

function DocumentRow({
  doc, onRetry, onUploadFor,
}: {
  doc: TenderDoc;
  onRetry: (id: number) => void;
  onUploadFor: (doc: TenderDoc) => void;
}) {
  const status = STATUS_CONFIG[doc.extraction_status] || STATUS_CONFIG.pending;
  const StatusIcon = status.icon;
  const typeLabel = TYPE_LABELS[doc.document_type] || doc.document_type;

  return (
    <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-border hover:border-border bg-card transition-colors">
      <FileText size={16} className="text-muted-foreground flex-shrink-0" />

      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-foreground truncate">{doc.file_name}</p>
        <div className="flex items-center gap-2 mt-0.5">
          <span className="text-[11px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
            {typeLabel}
          </span>
          {doc.gem_file_id && (
            <span className="text-[11px] text-muted-foreground">ID: {doc.gem_file_id}</span>
          )}
          {doc.file_size > 0 && (
            <span className="text-[11px] text-muted-foreground">{formatSize(doc.file_size)}</span>
          )}
        </div>
      </div>

      {/* Status Badge */}
      <div className={`flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded-md ${status.color}`}>
        <StatusIcon size={11} className={doc.extraction_status === 'processing' ? 'animate-spin' : ''} />
        {status.label}
      </div>

      {/* Actions */}
      {(doc.extraction_status === 'failed' || doc.extraction_status === 'failed_auth') && doc.gem_file_id && (
        <div className="flex items-center gap-1">
          <button
            onClick={() => onRetry(doc.id)}
            className="text-[11px] px-2 py-1 bg-accent/10 text-accent hover:bg-accent/15 rounded transition-colors"
          >
            Retry
          </button>
          <button
            onClick={() => onUploadFor(doc)}
            className="text-[11px] px-2 py-1 bg-orange-50 dark:bg-orange-500/15 text-orange-700 dark:text-orange-400 hover:bg-orange-100 dark:bg-orange-500/20 rounded transition-colors"
          >
            Upload
          </button>
        </div>
      )}
    </div>
  );
}
