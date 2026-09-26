import { useState, useEffect, useRef, useMemo } from 'react';
import {
  Upload, FileText, Pencil, Trash2, X, ChevronDown, ChevronRight,
  Eye, Shield, Clock, FileSpreadsheet, File, Plus, Search,
  Columns, CheckSquare, Calculator, FolderOpen, Lock,
} from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getUnifiedTemplates,
  uploadTemplateUnified,
  updateTemplate,
  deleteTemplate,
  updateDocumentTemplate,
  deleteDocumentTemplate,
  updateCostingTemplate,
  deleteCostingTemplate,
  getTemplatePreview,
} from '../../lib/api';

// ── Types ──────────────────────────────────────────────────────────────────

type TemplateKind = 'proposal' | 'document' | 'costing';
type TabFilter = 'all' | TemplateKind;

interface UnifiedTemplate {
  id: number;
  template_kind: TemplateKind;
  name: string;
  description: string | null;
  category: string | null;
  source: string | null;
  is_system: boolean;
  is_active: boolean;
  output_format: string;
  original_file_name: string | null;
  structure_summary: Record<string, number> | null;
  metadata: Record<string, any> | null;
  created_at: string | null;
  updated_at: string | null;
}

// ── Constants ──────────────────────────────────────────────────────────────

const KIND_CONFIG: Record<TemplateKind, { label: string; color: string; bg: string; icon: any }> = {
  proposal: { label: 'Proposal', color: 'text-accent', bg: 'bg-accent/10 border-accent/20', icon: FolderOpen },
  document: { label: 'Document', color: 'text-emerald-700 dark:text-emerald-400', bg: 'bg-emerald-50 dark:bg-emerald-500/15 border-emerald-200 dark:border-emerald-500/20', icon: FileText },
  costing: { label: 'Costing', color: 'text-amber-700 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-500/15 border-amber-200 dark:border-amber-500/20', icon: Calculator },
};

const FORMAT_CONFIG: Record<string, { label: string; color: string }> = {
  docx: { label: 'DOCX', color: 'bg-accent/15 text-accent' },
  xlsx: { label: 'XLSX', color: 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400' },
  pdf: { label: 'PDF', color: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400' },
};

const DOCUMENT_CATEGORIES = [
  'annexure', 'declaration', 'certificate', 'boq', 'letter', 'proposal', 'custom',
];

const COSTING_FORMAT_TYPES = ['boq', 'rate_schedule', 'cost_statement', 'price_bid'];

// ── Confirmation Dialog ────────────────────────────────────────────────────

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

// ── Upload Modal ───────────────────────────────────────────────────────────

function UploadModal({ onClose, onUploaded }: { onClose: () => void; onUploaded: () => void }) {
  const [kind, setKind] = useState<TemplateKind>('document');
  const [category, setCategory] = useState('');
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const acceptedFormats = kind === 'costing' ? '.pdf,.xlsx,.xls' : '.pdf,.docx';

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError('');
    try {
      await uploadTemplateUnified(file, kind, category || undefined);
      onUploaded();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Upload failed');
    }
    setUploading(false);
    if (fileRef.current) fileRef.current.value = '';
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-card rounded-xl w-full max-w-md shadow-xl">
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <h3 className="text-sm font-semibold text-foreground">Upload Template</h3>
          <button onClick={onClose} className="text-muted-foreground hover:text-muted-foreground"><X size={18} /></button>
        </div>
        <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
          {/* Template Type */}
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-2">Template Type</label>
            <div className="grid grid-cols-3 gap-2">
              {(['proposal', 'document', 'costing'] as TemplateKind[]).map((k) => {
                const cfg = KIND_CONFIG[k];
                const Icon = cfg.icon;
                return (
                  <button
                    key={k}
                    onClick={() => { setKind(k); setCategory(''); }}
                    className={`flex flex-col items-center gap-1.5 p-3 rounded-lg border-2 text-sm transition-all ${
                      kind === k ? `${cfg.bg} border-current ${cfg.color}` : 'border-border text-muted-foreground hover:border-border'
                    }`}
                  >
                    <Icon size={18} />
                    <span className="font-medium">{cfg.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Category (for document/costing) */}
          {kind === 'document' && (
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Document Category</label>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="w-full border border-border rounded-lg px-3 py-2 text-sm"
              >
                <option value="">Auto-detect from content</option>
                {DOCUMENT_CATEGORIES.map((c) => (
                  <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
                ))}
              </select>
            </div>
          )}

          {/* File Upload */}
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Template File</label>
            <div className="border-2 border-dashed border-border rounded-lg p-6 text-center hover:border-accent/60 transition-colors cursor-pointer"
              onClick={() => fileRef.current?.click()}
            >
              <Upload size={24} className="mx-auto text-muted-foreground mb-2" />
              <p className="text-sm text-muted-foreground">
                Click to select a file
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                {kind === 'costing' ? 'PDF, XLSX' : 'PDF, DOCX'}
              </p>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept={acceptedFormats}
              onChange={handleUpload}
              className="hidden"
            />
          </div>

          {uploading && (
            <div className="flex items-center gap-2 text-sm text-accent">
              <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
              Uploading & parsing template...
            </div>
          )}
          {error && (
            <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15 px-3 py-2 rounded-lg">{error}</div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Preview Modal ──────────────────────────────────────────────────────────

function PreviewModal({ template, onClose }: { template: UnifiedTemplate; onClose: () => void }) {
  const [preview, setPreview] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    getTemplatePreview(template.template_kind, template.id)
      .then(setPreview)
      .catch(() => setPreview(null))
      .finally(() => setLoading(false));
  }, [template.id, template.template_kind]);

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-card rounded-xl w-full max-w-2xl shadow-xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-border flex-shrink-0">
          <div>
            <h3 className="text-sm font-semibold text-foreground">{template.name}</h3>
            <p className="text-xs text-muted-foreground">{template.template_kind} template preview</p>
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-muted-foreground"><X size={18} /></button>
        </div>
        <div className="p-6 overflow-y-auto flex-1">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
            </div>
          ) : !preview ? (
            <p className="text-sm text-muted-foreground text-center py-8">No preview available</p>
          ) : template.template_kind === 'proposal' ? (
            <ProposalPreview preview={preview} />
          ) : template.template_kind === 'document' ? (
            <DocumentPreview preview={preview} />
          ) : (
            <CostingPreview preview={preview} />
          )}
        </div>
      </div>
    </div>
  );
}

function ProposalPreview({ preview }: { preview: any }) {
  return (
    <div className="space-y-3">
      {preview.metadata && (
        <div className="flex flex-wrap gap-2 mb-4">
          {preview.metadata.contract_type && (
            <span className="text-xs bg-accent/10 text-accent px-2 py-0.5 rounded-full">{preview.metadata.contract_type}</span>
          )}
          {preview.metadata.bidding_system && (
            <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full">{preview.metadata.bidding_system}</span>
          )}
          {preview.metadata.railway_zone && (
            <span className="text-xs bg-purple-50 dark:bg-purple-500/15 text-purple-600 dark:text-purple-400 px-2 py-0.5 rounded-full">{preview.metadata.railway_zone}</span>
          )}
        </div>
      )}
      {(preview.sections || []).map((s: any, i: number) => (
        <div key={i} className="border border-border rounded-lg p-3">
          <h4 className="text-sm font-semibold text-foreground mb-1">{s.title}</h4>
          {s.description && <p className="text-xs text-muted-foreground mb-2">{s.description}</p>}
          {s.fields?.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-1">
              {s.fields.map((f: any, j: number) => (
                <span key={j} className="text-[10px] bg-muted text-muted-foreground px-1.5 py-0.5 rounded">
                  {f.label || f.name} ({f.type}){f.required ? ' *' : ''}
                </span>
              ))}
            </div>
          )}
          {s.tables?.length > 0 && (
            <div className="mt-2">
              {s.tables.map((t: any, j: number) => (
                <div key={j} className="text-xs text-muted-foreground">
                  <Columns size={10} className="inline mr-1" />
                  Table: {t.name} — {(t.columns || []).join(', ')}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
      {(!preview.sections || preview.sections.length === 0) && (
        <p className="text-sm text-muted-foreground text-center py-4">No sections parsed. Try re-uploading the template.</p>
      )}
    </div>
  );
}

function DocumentPreview({ preview }: { preview: any }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <span className="text-xs bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 px-2 py-0.5 rounded-full">{preview.category}</span>
        {(preview.match_patterns || []).map((p: string, i: number) => (
          <span key={i} className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full font-mono">{p}</span>
        ))}
      </div>

      {preview.format_rules?.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Format Rules</h4>
          <ul className="space-y-1">
            {preview.format_rules.map((r: string, i: number) => (
              <li key={i} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                <CheckSquare size={10} className="mt-0.5 text-emerald-500 flex-shrink-0" />
                {r}
              </li>
            ))}
          </ul>
        </div>
      )}

      {preview.content_template && (
        <div>
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Template Preview</h4>
          <pre className="text-xs text-muted-foreground bg-muted/40 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto border border-border">
            {preview.content_template}
          </pre>
        </div>
      )}
    </div>
  );
}

function CostingPreview({ preview }: { preview: any }) {
  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <span className="text-xs bg-amber-50 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400 px-2 py-0.5 rounded-full">{preview.zone}</span>
        <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full">{preview.format_type}</span>
      </div>

      {preview.columns?.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Columns</h4>
          <div className="border border-border rounded-lg overflow-hidden">
            <table className="w-full text-xs">
              <thead className="bg-muted/40">
                <tr>
                  <th className="px-3 py-1.5 text-left text-muted-foreground font-medium">Label</th>
                  <th className="px-3 py-1.5 text-left text-muted-foreground font-medium">Type</th>
                  <th className="px-3 py-1.5 text-left text-muted-foreground font-medium">Formula</th>
                </tr>
              </thead>
              <tbody>
                {preview.columns.map((c: any, i: number) => (
                  <tr key={i} className="border-t border-border">
                    <td className="px-3 py-1.5 font-medium text-foreground">{c.label}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{c.type}</td>
                    <td className="px-3 py-1.5 text-muted-foreground font-mono">{c.formula || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {preview.footer_rows?.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Footer Rows</h4>
          <div className="space-y-1">
            {preview.footer_rows.map((f: any, i: number) => (
              <div key={i} className="flex items-center justify-between text-xs bg-muted/40 rounded px-3 py-1.5">
                <span className="font-medium text-foreground">{f.label}</span>
                <span className="text-muted-foreground font-mono">{f.type}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview.sample_table?.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">Sample Table</h4>
          <div className="border border-border rounded-lg overflow-hidden overflow-x-auto">
            <table className="w-full text-xs">
              {preview.sample_table.map((row: string[], rIdx: number) => (
                <tr key={rIdx} className={rIdx === 0 ? 'bg-foreground text-background' : 'border-t border-border'}>
                  {row.map((cell: string, cIdx: number) => (
                    rIdx === 0
                      ? <th key={cIdx} className="px-3 py-1.5 text-left font-medium">{cell}</th>
                      : <td key={cIdx} className="px-3 py-1.5 text-muted-foreground">{cell}</td>
                  ))}
                </tr>
              ))}
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Edit Modal ─────────────────────────────────────────────────────────────

function EditModal({ template, onClose, onSaved }: { template: UnifiedTemplate; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(template.name);
  const [description, setDescription] = useState(template.description || '');
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      const body: Record<string, any> = { name, description };

      if (template.template_kind === 'proposal') {
        await updateTemplate(template.id, body);
      } else if (template.template_kind === 'document') {
        await updateDocumentTemplate(template.id, body);
      } else {
        await updateCostingTemplate(template.id, body);
      }

      onSaved();
      onClose();
    } catch {}
    setSaving(false);
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-card rounded-xl w-full max-w-md shadow-xl">
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <h3 className="text-sm font-semibold text-foreground">Edit Template</h3>
          <button onClick={onClose} className="text-muted-foreground hover:text-muted-foreground"><X size={18} /></button>
        </div>
        <div className="p-6 space-y-3">
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Name</label>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} className="w-full border border-border rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Description</label>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} className="w-full border border-border rounded-lg px-3 py-2 text-sm" rows={3} />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button onClick={onClose} className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-muted/40">Cancel</button>
            <button onClick={handleSave} disabled={saving} className="px-4 py-2 text-sm bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 disabled:opacity-50">
              {saving ? 'Saving...' : 'Update'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────

export default function TemplatesPage() {
  const [templates, setTemplates] = useState<UnifiedTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<TabFilter>('all');
  const [search, setSearch] = useState('');
  const [showUpload, setShowUpload] = useState(false);
  const [editTemplate, setEditTemplate] = useState<UnifiedTemplate | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UnifiedTemplate | null>(null);
  const [previewTemplate, setPreviewTemplate] = useState<UnifiedTemplate | null>(null);

  const fetchData = () => {
    setLoading(true);
    getUnifiedTemplates(activeTab === 'all' ? undefined : activeTab)
      .then(setTemplates)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchData(); }, [activeTab]);

  const filtered = useMemo(() => {
    if (!search.trim()) return templates;
    const q = search.toLowerCase();
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        (t.description || '').toLowerCase().includes(q) ||
        (t.category || '').toLowerCase().includes(q)
    );
  }, [templates, search]);

  const counts = useMemo(() => ({
    all: templates.length,
    proposal: templates.filter((t) => t.template_kind === 'proposal').length,
    document: templates.filter((t) => t.template_kind === 'document').length,
    costing: templates.filter((t) => t.template_kind === 'costing').length,
  }), [templates]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      if (deleteTarget.template_kind === 'proposal') {
        await deleteTemplate(deleteTarget.id);
      } else if (deleteTarget.template_kind === 'document') {
        await deleteDocumentTemplate(deleteTarget.id);
      } else {
        await deleteCostingTemplate(deleteTarget.id);
      }
    } catch {}
    setDeleteTarget(null);
    fetchData();
  };

  if (loading && templates.length === 0) return <><Header title="Template Management" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Template Management" />
      <div className="p-6">
        {/* Description + Upload button */}
        <div className="flex justify-between items-start mb-5">
          <div>
            <p className="text-sm text-muted-foreground max-w-xl">
              Manage all template types — proposal structures, document formats, and costing sheets.
              Templates govern how agents generate output for tender submissions.
            </p>
          </div>
          <button
            onClick={() => setShowUpload(true)}
            className="flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 flex-shrink-0"
          >
            <Plus size={14} /> Add Template
          </button>
        </div>

        {/* Tab Bar */}
        <div className="flex items-center gap-1 mb-4 border-b border-border">
          {(['all', 'proposal', 'document', 'costing'] as TabFilter[]).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab
                  ? 'border-accent text-accent'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab === 'all' ? 'All' : KIND_CONFIG[tab].label}
              <span className="ml-1.5 text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded-full">
                {counts[tab]}
              </span>
            </button>
          ))}

          {/* Search */}
          <div className="ml-auto flex items-center gap-1.5 bg-muted rounded-lg px-3 py-1.5 mb-1">
            <Search size={14} className="text-muted-foreground" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search templates..."
              className="bg-transparent text-sm text-foreground outline-none w-48"
            />
          </div>
        </div>

        {/* Template Grid */}
        {filtered.length === 0 ? (
          <div className="bg-card rounded-xl border border-border p-16 text-center">
            <FileText size={40} className="text-muted-foreground/40 mx-auto mb-3" />
            <h3 className="text-lg font-semibold text-foreground mb-1">
              {search ? 'No matching templates' : 'No Templates Yet'}
            </h3>
            <p className="text-sm text-muted-foreground mb-4">
              {search ? 'Try a different search term.' : 'Upload a template to get started.'}
            </p>
            {!search && (
              <button
                onClick={() => setShowUpload(true)}
                className="inline-flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90"
              >
                <Plus size={14} /> Add Template
              </button>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {filtered.map((t) => {
              const kind = KIND_CONFIG[t.template_kind];
              const fmt = FORMAT_CONFIG[t.output_format] || FORMAT_CONFIG.docx;
              const KindIcon = kind.icon;

              return (
                <div key={`${t.template_kind}-${t.id}`} className="bg-card rounded-lg border border-border p-5 hover:border-accent/40 hover:shadow-md transition-all">
                  <div className="flex items-start justify-between mb-2.5">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className={`w-10 h-10 rounded-lg ${kind.bg} border flex items-center justify-center flex-shrink-0`}>
                        <KindIcon size={18} className={kind.color} />
                      </div>
                      <div className="min-w-0">
                        <h4 className="text-sm font-semibold text-foreground truncate">{t.name}</h4>
                        <div className="flex items-center gap-1.5 mt-0.5">
                          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${kind.bg} border ${kind.color}`}>
                            {kind.label}
                          </span>
                          {t.category && (
                            <span className="text-[10px] text-muted-foreground">{t.category}</span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      {t.is_system && (
                        <span className="flex items-center gap-0.5 text-[10px] bg-purple-50 dark:bg-purple-500/15 text-purple-600 dark:text-purple-400 px-1.5 py-0.5 rounded-full font-medium">
                          <Lock size={8} /> System
                        </span>
                      )}
                      <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${fmt.color}`}>
                        {fmt.label}
                      </span>
                    </div>
                  </div>

                  {t.description && (
                    <p className="text-xs text-muted-foreground mb-2.5 line-clamp-2">{t.description}</p>
                  )}

                  {/* Structure summary */}
                  {t.structure_summary && (
                    <div className="flex flex-wrap gap-1.5 mb-2.5">
                      {Object.entries(t.structure_summary).map(([key, val]) => (
                        <span key={key} className="text-[10px] bg-muted text-muted-foreground px-1.5 py-0.5 rounded">
                          {val} {key}
                        </span>
                      ))}
                    </div>
                  )}

                  {t.original_file_name && (
                    <div className="flex items-center gap-1 text-[10px] text-muted-foreground mb-2.5">
                      <File size={10} />
                      <span className="truncate">{t.original_file_name}</span>
                    </div>
                  )}

                  <div className="flex items-center justify-between pt-2.5 border-t border-border">
                    <button
                      onClick={() => setPreviewTemplate(t)}
                      className="flex items-center gap-1 text-xs text-muted-foreground hover:text-accent"
                    >
                      <Eye size={12} /> Preview
                    </button>
                    <div className="flex items-center gap-1">
                      {!t.is_system && (
                        <>
                          <button onClick={() => setEditTemplate(t)} className="text-muted-foreground hover:text-accent p-1" title="Edit">
                            <Pencil size={14} />
                          </button>
                          <button onClick={() => setDeleteTarget(t)} className="text-muted-foreground hover:text-red-600 dark:text-red-400 p-1" title="Delete">
                            <Trash2 size={14} />
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Modals */}
      {showUpload && <UploadModal onClose={() => setShowUpload(false)} onUploaded={fetchData} />}
      {editTemplate && <EditModal template={editTemplate} onClose={() => setEditTemplate(null)} onSaved={fetchData} />}
      {deleteTarget && (
        <ConfirmDialog
          message={`Delete "${deleteTarget.name}"? This action cannot be undone.`}
          onConfirm={handleDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
      {previewTemplate && <PreviewModal template={previewTemplate} onClose={() => setPreviewTemplate(null)} />}
    </>
  );
}
