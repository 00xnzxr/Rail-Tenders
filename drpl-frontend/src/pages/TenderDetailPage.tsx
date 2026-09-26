import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, ExternalLink, Brain, FileText, Zap, RefreshCw, ClipboardList, MessageSquareText, Download } from 'lucide-react';
import Header from '../components/layout/Header';
import StatusBadge from '../components/ui/StatusBadge';
import PortalBadge from '../components/ui/PortalBadge';
import PriorityBadge from '../components/ui/PriorityBadge';
import WorkflowBadge from '../components/ui/WorkflowBadge';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { getTenderById, analyzeTender, updateTender, assignTender, getUsers, API_BASE, getTenderDocuments, getTenderDocumentViewUrl } from '../lib/api';
import type { TenderDocumentInfo } from '../lib/api';
import { formatCurrency, formatDateTime } from '../lib/formatters';
import { differenceInDays, parseISO } from 'date-fns';
import { useAuth } from '../context/AuthContext';
import type { TenderDetail } from '../types/tender';
import type { UserProfile } from '../types/auth';

export default function TenderDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { auth } = useAuth();
  const [tender, setTender] = useState<TenderDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [users, setUsers] = useState<UserProfile[]>([]);
  const [userRole, setUserRole] = useState<string>('operator');

  useEffect(() => {
    if (!id) return;
    getTenderById(Number(id))
      .then(setTender)
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load tender'))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    getUsers()
      .then((u) => {
        setUsers(u);
        const me = u.find((user) => user.id === auth.userId);
        if (me) setUserRole(me.role);
      })
      .catch(() => {});
  }, [auth.userId]);

  const isAdmin = userRole === 'admin';

  const handleAnalyze = async () => {
    if (!id) return;
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      await analyzeTender(Number(id));
      const updated = await getTenderById(Number(id));
      setTender(updated);
    } catch (err: any) {
      setAnalyzeError(err.response?.data?.detail || 'AI analysis failed. Check API key configuration.');
    } finally {
      setAnalyzing(false);
    }
  };

  const handlePriorityChange = async (newPriority: string) => {
    if (!id) return;
    try {
      const updated = await updateTender(Number(id), { priority: newPriority });
      setTender(updated);
    } catch {}
  };

  const handleAssign = async (userId: number) => {
    if (!id) return;
    try {
      const updated = await assignTender(Number(id), userId);
      setTender(updated);
    } catch {}
  };

  if (loading) return <><Header title="Tender Detail" /><LoadingSpinner /></>;

  if (error || !tender) {
    return (
      <>
        <Header title="Tender Detail" />
        <div className="p-6">
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-red-600 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400">
            {error || 'Tender not found'}
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <Header title="Tender overview" subtitle="Understand the opportunity and choose your next step" />
      <div className="mx-auto w-full max-w-[1400px] space-y-6 p-4 sm:p-6 lg:p-8">
        {/* Back button */}
        <button
          onClick={() => navigate(-1)}
          className="flex items-center gap-2 text-sm text-muted-foreground hover:text-drpl-primary transition-colors"
        >
          <ArrowLeft size={16} />
          Back
        </button>

        {/* Header card */}
        <div className="rounded-2xl border border-border bg-card p-5 shadow-card sm:p-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1">
              <p className="mb-2 text-xs font-extrabold uppercase tracking-[0.14em] text-emerald-700 dark:text-emerald-400">Tender opportunity</p>
              <h1 className="text-xl font-extrabold leading-snug tracking-tight text-foreground sm:text-2xl">{tender.title}</h1>
              <p className="text-sm text-muted-foreground font-mono mt-1">
                {tender.portal.toUpperCase()}-{tender.tender_id}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <PriorityBadge priority={tender.priority} />
              <PortalBadge portal={tender.portal} />
              <StatusBadge status={tender.status} />
            </div>
          </div>
        </div>

        {/* Admin Controls: Priority & Assignment */}
        {isAdmin && (
          <div className="bg-card rounded-lg border border-border p-6">
            <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-3">
              Admin Controls
            </h3>
            <div className="flex flex-wrap gap-4">
              <div>
                <label className="block text-xs text-muted-foreground mb-1">Priority</label>
                <select
                  value={tender.priority}
                  onChange={(e) => handlePriorityChange(e.target.value)}
                  className="border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                >
                  <option value="critical">Critical</option>
                  <option value="high">High</option>
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                </select>
              </div>
              <div>
                <label className="block text-xs text-muted-foreground mb-1">Assign To</label>
                <select
                  value={tender.assigned_to ?? ''}
                  onChange={(e) => e.target.value && handleAssign(Number(e.target.value))}
                  className="border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                >
                  <option value="">Unassigned</option>
                  {users.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name} ({u.email})
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {tender.assigned_to && tender.assigned_at && (
              <p className="text-xs text-muted-foreground mt-2">
                Assigned on {formatDateTime(tender.assigned_at)}
              </p>
            )}
          </div>
        )}

        {/* Action buttons */}
        <div className="flex flex-wrap gap-3">
          <button
            onClick={() => navigate(`/tenders/${id}/command-center`)}
            className="flex min-h-11 items-center gap-2 rounded-xl bg-emerald-600 px-5 py-2.5 text-sm font-bold text-white shadow-sm hover:-translate-y-px hover:bg-emerald-700 hover:shadow-md"
          >
            <MessageSquareText size={16} />
            Ask DRPL about this tender
          </button>
          <button
            onClick={() => navigate(`/tenders/${id}/checklist`)}
            className="flex min-h-11 items-center gap-2 rounded-xl border border-border bg-card px-5 py-2.5 text-sm font-bold hover:border-accent/40 hover:text-accent"
          >
            <ClipboardList size={16} />
            Prepare checklist
          </button>
          {tender.workflow_status === 'approved' && (
            <a
              href={`${API_BASE}/api/tenders/${id}/download-zip`}
              className="flex items-center gap-2 bg-emerald-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-emerald-700 transition-colors"
              onClick={(e) => {
                e.preventDefault();
                const token = localStorage.getItem('drpl_token');
                fetch(`${API_BASE}/api/tenders/${id}/download-zip`, {
                  headers: { Authorization: `Bearer ${token}` },
                })
                  .then((res) => res.blob())
                  .then((blob) => {
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `DRPL_${tender.portal.toUpperCase()}_tender.zip`;
                    a.click();
                    window.URL.revokeObjectURL(url);
                  });
              }}
            >
              <Download size={16} />
              Download ZIP
            </a>
          )}
        </div>

        {/* Info grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <InfoCard label="Department" value={tender.department || '-'} />
          <InfoCard label="Organisation" value={tender.organisation || '-'} />
          <InfoCard label="Estimated Value" value={formatCurrency(tender.estimated_value, tender.currency || 'INR')} />
          <InfoCard label="EMD Amount" value={formatCurrency(tender.emd_amount, tender.currency || 'INR')} />
          <InfoCard label="Currency" value={tender.currency || 'INR'} />
          <InfoCard label="Opening Date" value={formatDateTime(tender.opening_date)} />
          <InfoCard label="Closing Date" value={
            tender.closing_date
              ? `${formatDateTime(tender.closing_date)} (${(() => {
                  const days = differenceInDays(parseISO(tender.closing_date), new Date());
                  if (days < 0) return 'Expired';
                  if (days === 0) return 'Today';
                  return `${days} days left`;
                })()})`
              : '-'
          } />
          <InfoCard label="Pre-Bid Date" value={formatDateTime(tender.pre_bid_date)} />
          {tender.submission_deadline && (
            <InfoCard label="Submission Deadline" value={formatDateTime(tender.submission_deadline)} />
          )}
        </div>

        {/* Description */}
        {tender.description && (
          <div className="bg-card rounded-lg border border-border p-6">
            <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-3">
              Description
            </h3>
            <p className="text-sm text-foreground whitespace-pre-wrap">{tender.description}</p>
          </div>
        )}

        {/* Documents */}
        <TenderDocumentsSection tenderId={tender.id} documentLinks={tender.document_links} />

        {/* AI Analysis */}
        <div className="rounded-2xl border border-border bg-card p-5 shadow-card sm:p-6">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider flex items-center gap-2">
              <Brain size={16} />
              DRPL review
            </h3>
            <button
              onClick={handleAnalyze}
              disabled={analyzing}
              className="flex items-center gap-2 bg-accent text-accent-foreground px-4 py-1.5 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-50"
            >
              {analyzing ? (
                <><RefreshCw size={14} className="animate-spin" /> Analyzing...</>
              ) : tender.ai_category ? (
                <><RefreshCw size={14} /> Review again</>
              ) : (
                <><Zap size={14} /> Review with DRPL</>
              )}
            </button>
          </div>
          {analyzeError && (
            <div className="mb-3 px-4 py-2.5 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400">
              {analyzeError}
            </div>
          )}
          {tender.ai_category || tender.ai_relevance_score != null || tender.ai_summary ? (
            <>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
                <div className="bg-muted/40 rounded-lg p-3">
                  <p className="text-xs text-muted-foreground">Category</p>
                  <p className="text-sm font-semibold text-foreground mt-1">{tender.ai_category || '-'}</p>
                </div>
                <div className="bg-muted/40 rounded-lg p-3">
                  <p className="text-xs text-muted-foreground">Relevance Score</p>
                  {tender.ai_relevance_score != null ? (
                    <RelevanceBar score={tender.ai_relevance_score} />
                  ) : (
                    <p className="text-sm font-medium text-foreground mt-1">-</p>
                  )}
                </div>
                <div className="bg-muted/40 rounded-lg p-3">
                  <p className="text-xs text-muted-foreground">Risk Score</p>
                  {tender.ai_risk_score != null ? (
                    <RiskBar score={tender.ai_risk_score} />
                  ) : (
                    <p className="text-sm font-medium text-foreground mt-1">-</p>
                  )}
                </div>
              </div>
              {tender.ai_summary && (
                <div className="bg-accent/10 border border-accent/20 rounded-lg p-4">
                  <p className="text-xs text-accent font-medium uppercase tracking-wider mb-1">DRPL summary</p>
                  <p className="text-sm text-foreground leading-relaxed">{tender.ai_summary}</p>
                </div>
              )}
            </>
          ) : (
            <div className="text-center py-6">
              <Brain size={32} className="mx-auto text-muted-foreground/50 mb-2" />
              <p className="text-sm text-muted-foreground">
                  No review yet. Ask DRPL to summarize the fit, risks, and important requirements.
              </p>
            </div>
          )}
        </div>

        {/* Metadata */}
        <div className="rounded-2xl border border-border bg-card p-5 shadow-card sm:p-6">
          <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-3">
            Source information
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <p className="text-xs text-muted-foreground">Source URL</p>
              {tender.source_url ? (
                <a
                  href={tender.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sm text-drpl-secondary hover:underline flex items-center gap-1"
                >
                  <ExternalLink size={12} />
                  View on portal
                </a>
              ) : (
                <p className="text-sm text-muted-foreground">-</p>
              )}
            </div>
            <InfoItem label="Extracted At" value={formatDateTime(tender.extracted_at)} />
            <InfoItem label="Created At" value={formatDateTime(tender.created_at)} />
            <InfoItem label="Updated At" value={formatDateTime(tender.updated_at)} />
          </div>
        </div>
      </div>
    </>
  );
}

const DOC_TYPE_LABELS: Record<string, string> = {
  nit: 'NIT', tender_notice: 'NIT', corrigendum: 'Corrigendum', amendment: 'Amendment',
  specification: 'Specification', other: 'Document', manual_upload: 'Upload',
  linked_document: 'Linked', gem_referenced: 'GEM',
};

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function TenderDocumentsSection({ tenderId, documentLinks }: { tenderId: number; documentLinks?: string[] }) {
  const [uploadedDocs, setUploadedDocs] = useState<TenderDocumentInfo[]>([]);
  const [previewDoc, setPreviewDoc] = useState<TenderDocumentInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const { auth } = useAuth();
  const [polling, setPolling] = useState(false);

  useEffect(() => {
    let lastCount = 0;
    let stableTicks = 0;
    let mounted = true;

    const tick = async () => {
      try {
        const res = await getTenderDocuments(tenderId);
        if (!mounted) return;
        setUploadedDocs(res.documents);
        const count = res.documents.length;
        if (count > lastCount) {
          stableTicks = 0; // new docs arriving — keep polling
          setPolling(true);
        } else {
          stableTicks++;
        }
        lastCount = count;
        setLoading(false);

        // Stop polling after 10 stable ticks (10 * 3s = 30s of no new docs)
        if (stableTicks >= 10) {
          setPolling(false);
          return;
        }
        // Continue polling
        setTimeout(tick, 3000);
      } catch {
        if (mounted) setLoading(false);
      }
    };

    tick();
    return () => { mounted = false; };
  }, [tenderId]);

  const hasUploaded = uploadedDocs.length > 0;
  const hasLinks = documentLinks && documentLinks.length > 0;

  if (loading) {
    return (
      <div className="bg-card rounded-lg border border-border p-6">
        <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-3 flex items-center gap-2">
          <FileText size={16} /> Documents
        </h3>
        <p className="text-sm text-muted-foreground">Loading documents...</p>
      </div>
    );
  }

  if (!hasUploaded && !hasLinks) return null;

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-3 flex items-center gap-2">
        <FileText size={16} /> Documents
        {polling && (
          <span className="ml-auto flex items-center gap-1 text-[11px] font-normal text-accent normal-case tracking-normal">
            <span className="inline-block w-1.5 h-1.5 bg-accent rounded-full animate-pulse" />
            Uploading more documents...
          </span>
        )}
      </h3>

      {hasUploaded && (
        <div className="space-y-2 mb-4">
          {uploadedDocs.map((doc) => (
            <button
              key={doc.id}
              onClick={() => setPreviewDoc(previewDoc?.id === doc.id ? null : doc)}
              className={`w-full flex items-center gap-3 p-3 rounded-lg border text-left transition-colors ${
                previewDoc?.id === doc.id
                  ? 'border-accent/40 bg-accent/10'
                  : 'border-border hover:bg-muted'
              }`}
            >
              <FileText size={18} className="text-red-500 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-foreground truncate">{doc.file_name}</p>
                <p className="text-xs text-muted-foreground">
                  {formatFileSize(doc.file_size)}
                  {doc.document_type && (
                    <span className="ml-2 px-1.5 py-0.5 bg-muted rounded text-muted-foreground">
                      {DOC_TYPE_LABELS[doc.document_type] || doc.document_type}
                    </span>
                  )}
                </p>
              </div>
            </button>
          ))}
        </div>
      )}

      {previewDoc && (
        <div className="mb-4 rounded-lg border border-border overflow-hidden">
          <div className="bg-muted/40 px-4 py-2 flex items-center justify-between border-b border-border">
            <span className="text-sm font-medium text-muted-foreground truncate">{previewDoc.file_name}</span>
            <button
              onClick={() => setPreviewDoc(null)}
              className="text-xs text-muted-foreground hover:text-muted-foreground"
            >
              Close
            </button>
          </div>
          <iframe
            src={`${getTenderDocumentViewUrl(tenderId, previewDoc.id)}${auth.token ? `?token=${auth.token}` : ''}`}
            className="w-full border-0"
            style={{ height: '600px' }}
            title={previewDoc.file_name}
          />
        </div>
      )}

      {hasLinks && !hasUploaded && (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground mb-2">External document links (PDFs not yet downloaded):</p>
          {documentLinks!.map((link, i) => (
            <a
              key={i}
              href={link}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 text-sm text-drpl-secondary hover:underline"
            >
              <ExternalLink size={14} />
              {link}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function InfoCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-card rounded-lg border border-border p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm font-medium text-foreground mt-1">{value}</p>
    </div>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm text-foreground">{value}</p>
    </div>
  );
}

function RelevanceBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color = pct >= 70 ? 'bg-emerald-500' : pct >= 40 ? 'bg-yellow-500' : 'bg-red-500';
  const textColor = pct >= 70 ? 'text-emerald-600' : pct >= 40 ? 'text-yellow-600' : 'text-red-600';
  return (
    <div className="mt-1">
      <p className={`text-lg font-bold ${textColor}`}>{pct}%</p>
      <div className="w-full bg-muted rounded-full h-1.5 mt-1">
        <div className={`${color} h-1.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function RiskBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color = pct >= 70 ? 'bg-red-500' : pct >= 40 ? 'bg-yellow-500' : 'bg-emerald-500';
  const textColor = pct >= 70 ? 'text-red-600' : pct >= 40 ? 'text-yellow-600' : 'text-emerald-600';
  const label = pct >= 70 ? 'High' : pct >= 40 ? 'Medium' : 'Low';
  return (
    <div className="mt-1">
      <p className={`text-lg font-bold ${textColor}`}>{label} ({pct}%)</p>
      <div className="w-full bg-muted rounded-full h-1.5 mt-1">
        <div className={`${color} h-1.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
