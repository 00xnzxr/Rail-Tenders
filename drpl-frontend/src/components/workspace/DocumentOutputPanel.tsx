import { useEffect, useRef, useState } from 'react';
import {
  FileText, PenTool, Download, FileOutput, Loader2, RefreshCw,
} from 'lucide-react';
import SignaturePlacementBoard from '../editor/SignaturePlacementBoard';
import {
  getLetterheadTemplates, getSignatures,
  generateWorkspaceDocumentPdf, downloadDocument, previewDocument,
} from '../../lib/api';

const PAGE_OPTIONS = [
  { value: 'last', label: 'Last Page' },
  { value: 'first', label: 'First Page' },
  { value: 'all', label: 'All Pages' },
  { value: 'custom', label: 'Specific Page' },
];

type SignatureConfig = {
  signature_id: number;
  position: string;
  position_x?: number;
  position_y?: number;
  page: string | number;
};

interface Props {
  tenderId: number;
  itemId: number;
  itemName: string;
  letterheadTemplateId: number | null;
  signatureConfigs: SignatureConfig[];
  generatedDocumentId: number | null;
  onLetterheadChange: (id: number | null) => void;
  onSignaturesChange: (configs: SignatureConfig[]) => void;
  onGenerated: (generatedDocumentId: number) => void;
  /** Called before each PDF generation so the parent can flush unsaved
   *  editor content to the server. */
  onBeforeGenerate?: () => Promise<void> | void;
}

export default function DocumentOutputPanel({
  tenderId, itemId, itemName,
  letterheadTemplateId, signatureConfigs,
  onLetterheadChange, onSignaturesChange, onGenerated,
  onBeforeGenerate,
}: Props) {
  const [tab, setTab] = useState<'settings' | 'preview'>('settings');
  const [templates, setTemplates] = useState<any[]>([]);
  const [signatures, setSignatures] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState('');
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const previewUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [tpls, sigs] = await Promise.all([getLetterheadTemplates(), getSignatures()]);
        if (cancelled) return;
        setTemplates(tpls);
        setSignatures(sigs);
      } catch (err: any) {
        if (!cancelled) setError(err.response?.data?.detail || 'Failed to load output settings');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Revoke object URLs on unmount / replacement
  useEffect(() => () => {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
  }, []);

  const setPreview = (blob: Blob | null) => {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    if (!blob) {
      previewUrlRef.current = null;
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(blob);
    previewUrlRef.current = url;
    setPreviewUrl(url);
  };

  const isSignatureSelected = (sigId: number) =>
    signatureConfigs.some((s) => s.signature_id === sigId);

  const getSignatureConfig = (sigId: number) =>
    signatureConfigs.find((s) => s.signature_id === sigId);

  const toggleSignature = (sigId: number) => {
    const exists = signatureConfigs.find((s) => s.signature_id === sigId);
    if (exists) {
      onSignaturesChange(signatureConfigs.filter((s) => s.signature_id !== sigId));
      return;
    }
    const sig = signatures.find((s: any) => s.id === sigId);
    onSignaturesChange([
      ...signatureConfigs,
      {
        signature_id: sigId,
        position: sig?.default_position || 'bottom-right',
        position_x: sig?.default_position_x,
        position_y: sig?.default_position_y,
        page: 'last',
      },
    ]);
  };

  const updateSignatureConfig = (sigId: number, updates: Partial<SignatureConfig>) => {
    onSignaturesChange(
      signatureConfigs.map((s) => (s.signature_id === sigId ? { ...s, ...updates } : s))
    );
  };

  const triggerBlobDownload = (blob: Blob, fileName: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const safeFileName = (ext: string) => {
    const base = (itemName || 'document').replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '_').slice(0, 80) || 'document';
    return `${base}.${ext}`;
  };

  const ensureGenerated = async (): Promise<number> => {
    if (onBeforeGenerate) await onBeforeGenerate();
    const result = await generateWorkspaceDocumentPdf(tenderId, itemId);
    onGenerated(result.generated_document_id);
    return result.generated_document_id;
  };

  const handleGenerate = async () => {
    setGenerating(true);
    setError('');
    try {
      const docId = await ensureGenerated();
      const blob = await previewDocument(docId);
      setPreview(blob);
      setTab('preview');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'PDF generation failed');
    } finally {
      setGenerating(false);
    }
  };

  const handlePreview = async () => {
    setPreviewing(true);
    setError('');
    try {
      const docId = await ensureGenerated();
      const blob = await previewDocument(docId);
      setPreview(blob);
      setTab('preview');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Preview failed');
    } finally {
      setPreviewing(false);
    }
  };

  const handleDownload = async () => {
    setDownloading(true);
    setError('');
    try {
      const docId = await ensureGenerated();
      const blob = await downloadDocument(docId);
      triggerBlobDownload(blob, safeFileName('pdf'));
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Download failed');
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="bg-card border rounded-xl flex flex-col h-full overflow-hidden">
      {/* Header + tabs */}
      <div className="border-b">
        <div className="flex items-center gap-2 px-4 pt-3">
          <FileOutput size={16} className="text-indigo-600 dark:text-indigo-400" />
          <h3 className="text-sm font-semibold text-foreground">Document Output</h3>
        </div>
        <div className="flex mt-2 px-2 gap-1">
          <button
            onClick={() => setTab('settings')}
            className={`px-3 py-1.5 text-xs font-medium rounded-t-md border-b-2 ${
              tab === 'settings' ? 'border-indigo-600 text-indigo-700 dark:text-indigo-400' : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            Settings
          </button>
          <button
            onClick={() => setTab('preview')}
            className={`px-3 py-1.5 text-xs font-medium rounded-t-md border-b-2 ${
              tab === 'preview' ? 'border-indigo-600 text-indigo-700 dark:text-indigo-400' : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            Preview
          </button>
        </div>
      </div>

      {error && (
        <div className="m-3 mb-0 p-2 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded text-xs text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4">
        {tab === 'settings' && (
          <div className="space-y-5">
            {/* Letterhead */}
            <div>
              <label className="flex items-center gap-1.5 text-xs font-semibold text-foreground mb-1.5">
                <FileText size={12} /> Letterhead
              </label>
              <select
                value={letterheadTemplateId ?? ''}
                disabled={loading}
                onChange={(e) => {
                  const v = e.target.value;
                  onLetterheadChange(v ? Number(v) : null);
                }}
                className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              >
                <option value="">No letterhead (plain)</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}{t.is_default ? ' (default)' : ''}
                  </option>
                ))}
              </select>
              {!loading && templates.length === 0 && (
                <p className="text-[10px] text-muted-foreground mt-1">
                  No letterhead templates. Add one under Letterheads in admin.
                </p>
              )}
            </div>

            {/* Signatures */}
            <div>
              <label className="flex items-center gap-1.5 text-xs font-semibold text-foreground mb-1.5">
                <PenTool size={12} /> Signatures
              </label>

              {loading ? (
                <p className="text-xs text-muted-foreground">Loading…</p>
              ) : signatures.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">
                  No signatures yet. Add one under the Signatures section.
                </p>
              ) : (
                <div className="space-y-2">
                  {signatures.map((sig) => {
                    const selected = isSignatureSelected(sig.id);
                    const config = getSignatureConfig(sig.id);
                    return (
                      <div
                        key={sig.id}
                        className={`rounded-lg border transition-colors ${
                          selected
                            ? 'border-indigo-300 bg-indigo-50/40'
                            : 'border-border hover:border-border'
                        }`}
                      >
                        <label className="flex items-center gap-3 p-2.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={selected}
                            onChange={() => toggleSignature(sig.id)}
                            className="rounded border-border"
                          />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-foreground truncate">{sig.name}</p>
                            {sig.designation && (
                              <p className="text-[11px] text-muted-foreground truncate">{sig.designation}</p>
                            )}
                          </div>
                        </label>

                        {selected && config && (
                          <div className="px-2.5 pb-2.5 pt-1 border-t border-border space-y-2">
                            <div>
                              <label className="block text-[10px] font-medium text-muted-foreground mb-0.5">Page</label>
                              <select
                                value={typeof config.page === 'number' ? 'custom' : config.page}
                                onChange={(e) => {
                                  const val = e.target.value;
                                  updateSignatureConfig(sig.id, {
                                    page: val === 'custom' ? 1 : val,
                                  });
                                }}
                                className="w-full border border-border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-indigo-500"
                              >
                                {PAGE_OPTIONS.map((p) => (
                                  <option key={p.value} value={p.value}>{p.label}</option>
                                ))}
                              </select>
                            </div>

                            {typeof config.page === 'number' && (
                              <div>
                                <label className="block text-[10px] font-medium text-muted-foreground mb-0.5">Page Number</label>
                                <input
                                  type="number"
                                  value={config.page}
                                  onChange={(e) => updateSignatureConfig(sig.id, { page: Math.max(1, Number(e.target.value)) })}
                                  min={1}
                                  className="w-full border border-border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-indigo-500"
                                />
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {signatureConfigs.length > 0 && (
                <div className="mt-4 border-t border-border pt-4 flex justify-center">
                  <SignaturePlacementBoard
                    signatures={signatures}
                    signatureConfigs={signatureConfigs}
                    onUpdateConfig={updateSignatureConfig}
                  />
                </div>
              )}
            </div>
          </div>
        )}

        {tab === 'preview' && (
          <div className="h-full flex flex-col">
            {previewUrl ? (
              <iframe
                src={previewUrl}
                className="w-full flex-1 rounded-lg border border-border bg-card"
                title="PDF Preview"
              />
            ) : (
              <div className="flex-1 flex items-center justify-center rounded-lg border border-dashed border-border bg-muted/40 text-center p-8">
                <div>
                  <FileOutput size={32} className="mx-auto text-muted-foreground/50 mb-2" />
                  <p className="text-xs text-muted-foreground">
                    Click "Generate PDF" or "Refresh Preview" to render the document.
                  </p>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Sticky actions */}
      <div className="border-t p-3 space-y-2 bg-card">
        <button
          onClick={handleGenerate}
          disabled={generating}
          className="w-full flex items-center justify-center gap-2 bg-emerald-600 text-white px-3 py-2 rounded-lg text-sm font-medium hover:bg-emerald-700 disabled:opacity-50"
        >
          {generating ? <Loader2 size={14} className="animate-spin" /> : <FileOutput size={14} />}
          {generating ? 'Generating…' : 'Generate PDF'}
        </button>

        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={handlePreview}
            disabled={previewing || generating}
            className="flex items-center justify-center gap-1.5 bg-card border border-border text-foreground px-3 py-2 rounded-lg text-xs font-medium hover:bg-muted/40 disabled:opacity-50"
          >
            {previewing ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            {previewUrl ? 'Refresh Preview' : 'Preview'}
          </button>
          <button
            onClick={handleDownload}
            disabled={downloading || generating}
            className="flex items-center justify-center gap-1.5 bg-indigo-600 text-white px-3 py-2 rounded-lg text-xs font-medium hover:bg-indigo-700 disabled:opacity-50"
          >
            {downloading ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
            Download
          </button>
        </div>

        <p className="text-[10px] text-muted-foreground text-center">
          Saved to the Documents section as {itemName?.slice(0, 40) || 'document'}.
        </p>
      </div>
    </div>
  );
}
