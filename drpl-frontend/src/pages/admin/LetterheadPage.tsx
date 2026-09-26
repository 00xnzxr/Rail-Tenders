import { useState, useEffect } from 'react';
import { Layout, Plus, Trash2, Upload, Image, Eye, Save, Check, FileText } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getLetterheadTemplates, createLetterheadTemplate, updateLetterheadTemplate,
  deleteLetterheadTemplate, uploadLetterheadAsset, uploadLetterheadPdf,
} from '../../lib/api';

const ASSET_TYPES = ['header', 'footer', 'watermark', 'logo'] as const;
const PAGE_SIZES = ['A4', 'Letter', 'Legal'] as const;
const LOGO_POSITIONS = ['left', 'center', 'right'] as const;
const FONT_FAMILIES = ['Helvetica', 'Times New Roman', 'Arial', 'Courier New', 'Georgia', 'Calibri'] as const;

interface TemplateForm {
  name: string;
  description: string;
  is_default: boolean;
  margin_top: number;
  margin_bottom: number;
  margin_left: number;
  margin_right: number;
  font_family: string;
  font_size_pt: number;
  page_size: string;
  logo_position: string;
}

const emptyForm: TemplateForm = {
  name: '',
  description: '',
  is_default: false,
  margin_top: 25,
  margin_bottom: 25,
  margin_left: 20,
  margin_right: 20,
  font_family: 'Helvetica',
  font_size_pt: 11,
  page_size: 'A4',
  logo_position: 'left',
};

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

export default function LetterheadPage() {
  const [templates, setTemplates] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<TemplateForm>(emptyForm);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [uploadingAsset, setUploadingAsset] = useState<string | null>(null);
  const [uploadingPdf, setUploadingPdf] = useState<number | null>(null);
  const [error, setError] = useState('');

  const fetchData = async () => {
    try {
      const data = await getLetterheadTemplates();
      setTemplates(data);
    } catch {
      setError('Failed to load letterhead templates');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, []);

  const openCreateForm = () => {
    setEditingId(null);
    setForm(emptyForm);
    setShowForm(true);
  };

  const openEditForm = (t: any) => {
    setEditingId(t.id);
    setForm({
      name: t.name || '',
      description: t.description || '',
      is_default: t.is_default || false,
      margin_top: t.margin_top_mm ?? t.margin_top ?? 25,
      margin_bottom: t.margin_bottom_mm ?? t.margin_bottom ?? 25,
      margin_left: t.margin_left_mm ?? t.margin_left ?? 20,
      margin_right: t.margin_right_mm ?? t.margin_right ?? 20,
      font_family: t.font_family || 'Helvetica',
      font_size_pt: t.font_size_pt ?? 11,
      page_size: t.page_size || 'A4',
      logo_position: t.logo_position || 'left',
    });
    setShowForm(true);
  };

  const handleSave = async () => {
    if (!form.name.trim()) return;
    setSaving(true);
    setError('');
    try {
      // Map frontend field names to backend field names
      const payload = {
        name: form.name,
        description: form.description,
        is_default: form.is_default,
        margin_top_mm: form.margin_top,
        margin_bottom_mm: form.margin_bottom,
        margin_left_mm: form.margin_left,
        margin_right_mm: form.margin_right,
        font_family: form.font_family,
        font_size_pt: form.font_size_pt,
        page_size: form.page_size,
        logo_position: form.logo_position,
      };
      if (editingId) {
        await updateLetterheadTemplate(editingId, payload);
      } else {
        await createLetterheadTemplate(payload);
      }
      setShowForm(false);
      setEditingId(null);
      setForm(emptyForm);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save template');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (deleteId == null) return;
    try {
      await deleteLetterheadTemplate(deleteId);
      setDeleteId(null);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete template');
    }
  };

  const handlePdfUpload = async (templateId: number, file: File) => {
    setUploadingPdf(templateId);
    try {
      await uploadLetterheadPdf(templateId, file);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to upload PDF letterhead');
    } finally {
      setUploadingPdf(null);
    }
  };

  const handleAssetUpload = async (templateId: number, assetType: string, file: File) => {
    setUploadingAsset(`${templateId}-${assetType}`);
    try {
      await uploadLetterheadAsset(templateId, assetType, file);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || `Failed to upload ${assetType}`);
    } finally {
      setUploadingAsset(null);
    }
  };

  const updateField = <K extends keyof TemplateForm>(key: K, value: TemplateForm[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  if (loading) return <><Header title="Letterhead Management" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Letterhead Management" subtitle="Manage letterhead templates for document generation" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {error && (
          <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-3 text-sm text-red-600 dark:text-red-400">{error}</div>
        )}

        {/* Actions bar */}
        <div className="flex justify-between items-center">
          <p className="text-sm text-muted-foreground">{templates.length} template{templates.length !== 1 ? 's' : ''}</p>
          <button
            onClick={openCreateForm}
            className="flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors"
          >
            <Plus size={14} /> Create New
          </button>
        </div>

        {/* Inline form */}
        {showForm && (
          <div className="bg-card rounded-xl shadow-card border border-border p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-foreground">
                {editingId ? 'Edit Template' : 'Create New Template'}
              </h3>
              <button onClick={() => { setShowForm(false); setEditingId(null); }} className="text-xs text-muted-foreground hover:text-muted-foreground">Cancel</button>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="col-span-2">
                <label className="block text-xs font-medium text-muted-foreground mb-1">Name</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={(e) => updateField('name', e.target.value)}
                  placeholder="Template name..."
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-xs font-medium text-muted-foreground mb-1">Description</label>
                <textarea
                  value={form.description}
                  onChange={(e) => updateField('description', e.target.value)}
                  placeholder="Template description..."
                  rows={2}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                />
              </div>
              <div className="col-span-2">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.is_default}
                    onChange={(e) => updateField('is_default', e.target.checked)}
                    className="rounded border-border text-accent focus:ring-drpl-secondary"
                  />
                  <span className="text-sm text-foreground">Set as default template</span>
                </label>
              </div>
            </div>

            {/* Margins */}
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-2">Margins (mm)</label>
              <div className="grid grid-cols-4 gap-3">
                {(['margin_top', 'margin_bottom', 'margin_left', 'margin_right'] as const).map((key) => (
                  <div key={key}>
                    <label className="block text-[10px] text-muted-foreground mb-0.5 capitalize">{key.replace('margin_', '')}</label>
                    <input
                      type="number"
                      value={form[key]}
                      onChange={(e) => updateField(key, Number(e.target.value))}
                      className="w-full border border-border rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                    />
                  </div>
                ))}
              </div>
            </div>

            {/* Font, size, page, logo */}
            <div className="grid grid-cols-4 gap-3">
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Font Family</label>
                <select
                  value={form.font_family}
                  onChange={(e) => updateField('font_family', e.target.value)}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                >
                  {FONT_FAMILIES.map((f) => <option key={f} value={f}>{f}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Font Size (pt)</label>
                <input
                  type="number"
                  value={form.font_size_pt}
                  onChange={(e) => updateField('font_size_pt', Number(e.target.value))}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Page Size</label>
                <select
                  value={form.page_size}
                  onChange={(e) => updateField('page_size', e.target.value)}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                >
                  {PAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">Logo Position</label>
                <select
                  value={form.logo_position}
                  onChange={(e) => updateField('logo_position', e.target.value)}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                >
                  {LOGO_POSITIONS.map((p) => <option key={p} value={p} className="capitalize">{p}</option>)}
                </select>
              </div>
            </div>

            <div className="flex justify-end pt-2">
              <button
                onClick={handleSave}
                disabled={saving || !form.name.trim()}
                className="flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-50"
              >
                <Save size={14} /> {saving ? 'Saving...' : editingId ? 'Update' : 'Create'}
              </button>
            </div>
          </div>
        )}

        {/* Template grid */}
        {templates.length === 0 ? (
          <div className="bg-card rounded-xl border border-border p-16 text-center">
            <Layout size={40} className="text-muted-foreground/40 mx-auto mb-3" />
            <h3 className="text-lg font-semibold text-foreground mb-1">No Letterhead Templates</h3>
            <p className="text-sm text-muted-foreground mb-4">Create your first letterhead template to get started.</p>
            <button
              onClick={openCreateForm}
              className="inline-flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90"
            >
              <Plus size={14} /> Create Template
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {templates.map((t) => (
              <div key={t.id} className="bg-card rounded-xl shadow-card border border-border p-5 hover:border-accent/40 hover:shadow-md transition-all">
                {/* Card header */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2.5">
                    <div className="w-10 h-10 rounded-lg bg-accent/10 flex items-center justify-center">
                      <Layout size={20} className="text-accent" />
                    </div>
                    <div>
                      <h4 className="text-sm font-semibold text-foreground">{t.name}</h4>
                      {t.font_family && (
                        <p className="text-xs text-muted-foreground">{t.font_family} {t.font_size_pt}pt</p>
                      )}
                    </div>
                  </div>
                  {t.is_default && (
                    <span className="flex items-center gap-1 text-[10px] bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 px-2 py-0.5 rounded-full font-medium">
                      <Check size={10} /> Default
                    </span>
                  )}
                </div>

                {t.description && (
                  <p className="text-xs text-muted-foreground mb-3 line-clamp-2">{t.description}</p>
                )}

                {/* Asset indicators */}
                <div className="flex flex-wrap gap-1.5 mb-3">
                  <span
                    className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                      t.has_letterhead_pdf
                        ? 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                        : 'bg-muted text-muted-foreground'
                    }`}
                  >
                    PDF letterhead
                  </span>
                  {ASSET_TYPES.map((asset) => (
                    <span
                      key={asset}
                      className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                        t[`has_${asset}`] || t[`${asset}_path`]
                          ? 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                          : 'bg-muted text-muted-foreground'
                      }`}
                    >
                      {asset}
                    </span>
                  ))}
                </div>

                {/* Page info */}
                <div className="flex flex-wrap gap-2 mb-3">
                  {t.page_size && (
                    <span className="text-[10px] bg-muted text-muted-foreground px-2 py-0.5 rounded-full">
                      {t.page_size}
                    </span>
                  )}
                  {t.logo_position && (
                    <span className="text-[10px] bg-muted text-muted-foreground px-2 py-0.5 rounded-full">
                      Logo: {t.logo_position}
                    </span>
                  )}
                </div>

                {/* PDF Letterhead Upload */}
                <div className="mb-3">
                  <label
                    className={`flex items-center gap-2 px-3 py-2 rounded-lg border-2 border-dashed cursor-pointer transition-colors ${
                      t.has_letterhead_pdf
                        ? 'border-emerald-300 bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 hover:bg-emerald-100 dark:bg-emerald-500/20'
                        : 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/15'
                    }`}
                  >
                    <FileText size={16} />
                    <span className="text-xs font-medium">
                      {uploadingPdf === t.id
                        ? 'Uploading PDF...'
                        : t.has_letterhead_pdf
                          ? 'PDF Letterhead Uploaded (click to replace)'
                          : 'Upload PDF Letterhead'}
                    </span>
                    <input
                      type="file"
                      accept=".pdf"
                      className="hidden"
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file) handlePdfUpload(t.id, file);
                        e.target.value = '';
                      }}
                    />
                  </label>
                </div>

                {/* Individual asset uploads (optional overrides) */}
                <div className="space-y-1.5 mb-3">
                  <p className="text-[10px] text-muted-foreground uppercase tracking-wider font-medium">Individual Assets (optional)</p>
                  {ASSET_TYPES.map((asset) => (
                    <label
                      key={asset}
                      className="flex items-center gap-2 text-xs text-muted-foreground hover:text-accent cursor-pointer transition-colors"
                    >
                      <Upload size={12} />
                      <span className="capitalize">
                        {uploadingAsset === `${t.id}-${asset}` ? `Uploading ${asset}...` : `Upload ${asset}`}
                      </span>
                      <input
                        type="file"
                        accept="image/*"
                        className="hidden"
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (file) handleAssetUpload(t.id, asset, file);
                          e.target.value = '';
                        }}
                      />
                    </label>
                  ))}
                </div>

                {/* Actions */}
                <div className="flex items-center justify-between pt-3 border-t border-border">
                  <button
                    onClick={() => openEditForm(t)}
                    className="flex items-center gap-1 text-xs text-muted-foreground hover:text-accent transition-colors"
                  >
                    <Eye size={12} /> Edit
                  </button>
                  <button
                    onClick={() => setDeleteId(t.id)}
                    className="text-muted-foreground hover:text-red-600 dark:text-red-400 p-1 transition-colors"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {deleteId != null && (
        <ConfirmDialog
          message="Are you sure you want to delete this letterhead template? This action cannot be undone."
          onConfirm={handleDelete}
          onCancel={() => setDeleteId(null)}
        />
      )}
    </>
  );
}
