import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  FileOutput, Download, Eye, Save, PenTool, Sparkles, X, ArrowLeft,
  RectangleHorizontal, RectangleVertical,
} from 'lucide-react';
import { marked } from 'marked';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import RichTextEditor from '../components/editor/RichTextEditor';
import SignaturePlacementBoard from '../components/editor/SignaturePlacementBoard';
import {
  getLetterheadTemplates, getSignatures, createDocument, getDocument,
  updateDocument, generateDocumentPdf, previewDocument, downloadDocument,
  getDocumentTypeTemplate, aiGenerateDocumentContent,
} from '../lib/api';

const PAGE_OPTIONS = [
  { value: 'last', label: 'Last Page' },
  { value: 'first', label: 'First Page' },
  { value: 'all', label: 'All Pages' },
  { value: 'custom', label: 'Specific Page' },
];

const DOCUMENT_TYPES = [
  { value: 'custom', label: 'Custom' },
  { value: 'proposal', label: 'Proposal' },
  { value: 'cost_statement', label: 'Cost Statement' },
  { value: 'letter', label: 'Letter' },
  { value: 'certificate', label: 'Certificate' },
];

type SignatureConfig = {
  signature_id: number;
  position: string;
  position_x?: number;
  position_y?: number;
  page: string | number;
};

export default function DocumentGeneratorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const isEdit = !!id;

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');

  // Data
  const [templates, setTemplates] = useState<any[]>([]);
  const [signatures, setSignatures] = useState<any[]>([]);

  // Document state
  const [docId, setDocId] = useState<number | null>(id ? Number(id) : null);
  const [title, setTitle] = useState('');
  const [documentType, setDocumentType] = useState('custom');
  const [letterheadId, setLetterheadId] = useState<number | ''>('');
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10));
  const [refNumber, setRefNumber] = useState('');
  const [addressee, setAddressee] = useState('');
  const [subject, setSubject] = useState('');
  const [content, setContent] = useState('');
  const [signatureConfigs, setSignatureConfigs] = useState<SignatureConfig[]>([]);
  const [pageOrientation, setPageOrientation] = useState<'portrait' | 'landscape'>('portrait');
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  // AI Generate modal state
  const [showAiModal, setShowAiModal] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiMode, setAiMode] = useState<'generate' | 'enhance'>('generate');
  const [aiGenerating, setAiGenerating] = useState(false);
  const [aiResult, setAiResult] = useState<string | null>(null);

  useEffect(() => {
    const init = async () => {
      try {
        const [tpls, sigs] = await Promise.all([
          getLetterheadTemplates(),
          getSignatures(),
        ]);
        setTemplates(tpls);
        setSignatures(sigs);

        if (isEdit && id) {
          const doc = await getDocument(Number(id));
          setTitle(doc.title || '');
          setDocumentType(doc.document_type || 'custom');
          setLetterheadId(doc.letterhead_template_id || '');
          setDate(doc.template_variables?.date || new Date().toISOString().slice(0, 10));
          setRefNumber(doc.template_variables?.ref_number || '');
          setAddressee(doc.template_variables?.addressee || '');
          setSubject(doc.template_variables?.subject || '');
          // Prefer HTML; convert markdown as fallback
          const rawHtml = doc.content_html ||
            (doc.content_markdown ? marked.parse(doc.content_markdown) as string : '');
          setContent(rawHtml);
          setPageOrientation(
            doc.page_orientation === 'landscape' ? 'landscape' : 'portrait',
          );
          setSignatureConfigs(
            (doc.signatures || []).map((s: any) => ({
              signature_id: s.signature_id,
              position: s.position || 'bottom-right',
              position_x: s.position_x,
              position_y: s.position_y,
              page: s.page || 'last',
            }))
          );
        }
      } catch (err: any) {
        setError(err.response?.data?.detail || 'Failed to load data');
      } finally {
        setLoading(false);
      }
    };
    init();
  }, [id, isEdit]);

  const buildPayload = () => ({
    title,
    document_type: documentType,
    letterhead_template_id: letterheadId || null,
    content_html: content,
    template_variables: { date, ref_number: refNumber, addressee, subject },
    signatures: signatureConfigs,
    page_orientation: pageOrientation,
  });

  const handleSave = async () => {
    setSaving(true);
    setError('');
    try {
      if (docId) {
        await updateDocument(docId, buildPayload());
      } else {
        const created = await createDocument(buildPayload());
        setDocId(created.id);
        navigate(`/documents/generate/${created.id}`, { replace: true });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to save document');
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    if (!docId) {
      setError('Save the document first before previewing');
      return;
    }
    setPreviewing(true);
    setError('');
    try {
      await updateDocument(docId, buildPayload());
      const blob = await previewDocument(docId);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(URL.createObjectURL(blob));
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to preview document');
    } finally {
      setPreviewing(false);
    }
  };

  const handleGenerate = async () => {
    if (!docId) {
      setError('Save the document first before generating PDF');
      return;
    }
    setGenerating(true);
    setError('');
    try {
      await updateDocument(docId, buildPayload());
      await generateDocumentPdf(docId);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to generate PDF');
    } finally {
      setGenerating(false);
    }
  };

  const handleDownload = async () => {
    if (!docId) {
      setError('Save the document first before downloading');
      return;
    }
    setDownloading(true);
    setError('');
    try {
      const blob = await downloadDocument(docId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${title || 'document'}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to download document');
    } finally {
      setDownloading(false);
    }
  };

  // --- Signature config helpers ---
  const toggleSignature = (sigId: number) => {
    setSignatureConfigs((prev) => {
      const exists = prev.find((s) => s.signature_id === sigId);
      if (exists) {
        return prev.filter((s) => s.signature_id !== sigId);
      }
      const sig = signatures.find((s: any) => s.id === sigId);
      return [
        ...prev,
        {
          signature_id: sigId,
          position: sig?.default_position || 'bottom-right',
          position_x: sig?.default_position_x,
          position_y: sig?.default_position_y,
          page: 'last',
        },
      ];
    });
  };

  const updateSignatureConfig = (sigId: number, updates: Partial<SignatureConfig>) => {
    setSignatureConfigs((prev) =>
      prev.map((s) => (s.signature_id === sigId ? { ...s, ...updates } : s))
    );
  };

  const isSignatureSelected = (sigId: number) =>
    signatureConfigs.some((s) => s.signature_id === sigId);

  const getSignatureConfig = (sigId: number) =>
    signatureConfigs.find((s) => s.signature_id === sigId);

  // --- Document type change ---
  const handleDocumentTypeChange = async (newType: string) => {
    setDocumentType(newType);
    if (newType === 'custom' || newType === 'letter' || newType === 'certificate') return;

    try {
      const template = await getDocumentTypeTemplate(newType);
      if (content.trim() && !window.confirm('This will replace your current content with the template. Continue?')) {
        return;
      }
      const html = marked.parse(template.content_markdown_template || '') as string;
      setContent(html);
      const defaults = template.default_template_variables || {};
      if (!refNumber && defaults.ref_number) setRefNumber(defaults.ref_number);
      if (!subject && defaults.subject) setSubject(defaults.subject);
    } catch {
      // Template not available — that's fine
    }
  };

  // --- AI Generate ---
  const handleAiGenerate = async () => {
    if (!aiPrompt.trim()) return;

    if (!docId) {
      setSaving(true);
      try {
        const created = await createDocument(buildPayload());
        setDocId(created.id);
        navigate(`/documents/generate/${created.id}`, { replace: true });
        await runAiGeneration(created.id);
      } catch (err: any) {
        setError(err.response?.data?.detail || 'Failed to save document');
        setSaving(false);
        return;
      }
      setSaving(false);
    } else {
      await runAiGeneration(docId);
    }
  };

  const runAiGeneration = async (targetDocId: number) => {
    setAiGenerating(true);
    try {
      await updateDocument(targetDocId, buildPayload());
      const result = await aiGenerateDocumentContent(targetDocId, {
        prompt: aiPrompt,
        mode: aiMode,
        document_type: documentType,
      });
      setAiResult(result.content);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'AI generation failed');
    } finally {
      setAiGenerating(false);
    }
  };

  const applyAiResult = () => {
    if (aiResult) {
      setContent(marked.parse(aiResult) as string);
      setAiResult(null);
      setShowAiModal(false);
      setAiPrompt('');
    }
  };

  if (loading) return <><Header title="Document Generator" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Document Generator" subtitle={isEdit ? `Editing document #${id}` : 'Create a new document'} />
      <div className="px-6 pt-4">
        <button onClick={() => navigate('/documents/library')} className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition">
          <ArrowLeft size={14} /> Back to Documents
        </button>
      </div>
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {error && (
          <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-3 text-sm text-red-600 dark:text-red-400">{error}</div>
        )}

        {/* Top bar */}
        <div className="bg-card rounded-xl shadow-card border border-border p-4">
          <div className="flex flex-wrap items-center gap-3">
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Document title..."
              className="flex-1 min-w-[200px] border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
            />
            <select
              value={documentType}
              onChange={(e) => handleDocumentTypeChange(e.target.value)}
              className="border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
            >
              {DOCUMENT_TYPES.map((dt) => (
                <option key={dt.value} value={dt.value}>{dt.label}</option>
              ))}
            </select>
            <select
              value={letterheadId}
              onChange={(e) => setLetterheadId(e.target.value ? Number(e.target.value) : '')}
              className="border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
            >
              <option value="">No letterhead</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setPageOrientation((o) => (o === 'landscape' ? 'portrait' : 'landscape'))}
              title={`Switch to ${pageOrientation === 'landscape' ? 'portrait' : 'landscape'} page orientation`}
              className="flex items-center gap-1.5 border border-border text-foreground bg-card px-3 py-2 rounded-lg text-sm font-medium hover:bg-muted/40 transition-colors"
            >
              {pageOrientation === 'landscape'
                ? <><RectangleHorizontal size={14} /> Landscape</>
                : <><RectangleVertical size={14} /> Portrait</>}
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-1.5 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-50"
            >
              <Save size={14} /> {saving ? 'Saving...' : 'Save'}
            </button>
            <button
              onClick={handlePreview}
              disabled={previewing || !docId}
              className="flex items-center gap-1.5 border border-border text-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-muted/40 transition-colors disabled:opacity-50"
            >
              <Eye size={14} /> {previewing ? 'Loading...' : 'Preview'}
            </button>
            <button
              onClick={handleGenerate}
              disabled={generating || !docId}
              className="flex items-center gap-1.5 bg-emerald-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-50"
            >
              <FileOutput size={14} /> {generating ? 'Generating...' : 'Generate PDF'}
            </button>
            <button
              onClick={handleDownload}
              disabled={downloading || !docId}
              className="flex items-center gap-1.5 border border-border text-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-muted/40 transition-colors disabled:opacity-50"
            >
              <Download size={14} /> {downloading ? 'Downloading...' : 'Download'}
            </button>
          </div>
        </div>

        {/* Main content area */}
        <div className="flex gap-4">
          {/* Left side - 60% */}
          <div className="w-[60%] space-y-4">
            {/* Template variables */}
            <div className="bg-card rounded-xl shadow-card border border-border p-5">
              <h3 className="text-sm font-semibold text-foreground mb-3">Template Variables</h3>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Date</label>
                  <input
                    type="date"
                    value={date}
                    onChange={(e) => setDate(e.target.value)}
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Reference Number</label>
                  <input
                    type="text"
                    value={refNumber}
                    onChange={(e) => setRefNumber(e.target.value)}
                    placeholder="e.g. REF-2026-001"
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                  />
                </div>
                <div className="col-span-2">
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Addressee</label>
                  <input
                    type="text"
                    value={addressee}
                    onChange={(e) => setAddressee(e.target.value)}
                    placeholder="Recipient name and address..."
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                  />
                </div>
                <div className="col-span-2">
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Subject</label>
                  <input
                    type="text"
                    value={subject}
                    onChange={(e) => setSubject(e.target.value)}
                    placeholder="Document subject..."
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
                  />
                </div>
              </div>
            </div>

            {/* Rich text editor */}
            <div className="bg-card rounded-xl shadow-card border border-border p-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-foreground">Document Content</h3>
                <button
                  onClick={() => { setShowAiModal(true); setAiResult(null); }}
                  className="flex items-center gap-1.5 bg-gradient-to-r from-violet-600 to-indigo-600 text-white px-3 py-1.5 rounded-lg text-xs font-medium hover:from-violet-700 hover:to-indigo-700 transition-all"
                >
                  <Sparkles size={12} /> Draft with DRPL
                </button>
              </div>
              <RichTextEditor
                value={content}
                onChange={setContent}
                placeholder="Start typing your document content..."
                minHeight={480}
              />
            </div>
          </div>

          {/* Right side - 40% */}
          <div className="w-[40%] space-y-4">
            {/* Signature selector */}
            <div className="bg-card rounded-xl shadow-card border border-border p-5">
              <h3 className="text-sm font-semibold text-foreground mb-3 flex items-center gap-2">
                <PenTool size={14} /> Signatures
              </h3>
              {signatures.length === 0 ? (
                <p className="text-xs text-muted-foreground">No signatures available. Add signatures in Settings.</p>
              ) : (
                <div className="space-y-2 mb-4">
                  {signatures.map((sig) => {
                    const selected = isSignatureSelected(sig.id);
                    const config = getSignatureConfig(sig.id);
                    return (
                      <div
                        key={sig.id}
                        className={`rounded-lg border transition-colors ${
                          selected
                            ? 'border-drpl-secondary bg-accent/10'
                            : 'border-border hover:border-border'
                        }`}
                      >
                        <label className="flex items-center gap-3 p-3 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={selected}
                            onChange={() => toggleSignature(sig.id)}
                            className="rounded border-border text-accent focus:ring-drpl-secondary"
                          />
                          <div>
                            <p className="text-sm font-medium text-foreground">{sig.name}</p>
                            {sig.designation && (
                              <p className="text-xs text-muted-foreground">{sig.designation}</p>
                            )}
                          </div>
                        </label>

                        {/* Per-signature page config */}
                        {selected && config && (
                          <div className="px-3 pb-3 pt-1 border-t border-border space-y-2">
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
                                className="w-full border border-border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-drpl-secondary"
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
                                  className="w-full border border-border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-drpl-secondary"
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

              {/* Visual placement board — shown when at least one signature is selected */}
              {signatureConfigs.length > 0 && (
                <div className="border-t border-border pt-4">
                  <SignaturePlacementBoard
                    signatures={signatures}
                    signatureConfigs={signatureConfigs}
                    onUpdateConfig={updateSignatureConfig}
                  />
                </div>
              )}
            </div>

            {/* PDF Preview */}
            <div className="bg-card rounded-xl shadow-card border border-border p-5">
              <h3 className="text-sm font-semibold text-foreground mb-3 flex items-center gap-2">
                <Eye size={14} /> PDF Preview
              </h3>
              {previewUrl ? (
                <iframe
                  src={previewUrl}
                  className="w-full rounded-lg border border-border"
                  style={{ height: '600px' }}
                  title="Document Preview"
                />
              ) : (
                <div className="w-full flex items-center justify-center rounded-lg border border-dashed border-border bg-muted/40 text-center p-12">
                  <div>
                    <FileOutput size={32} className="mx-auto text-muted-foreground/50 mb-2" />
                    <p className="text-xs text-muted-foreground">
                      Save the document and click "Preview" to see the PDF here
                    </p>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* AI Generate Modal */}
      {showAiModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-card rounded-xl shadow-xl w-[600px] max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between p-4 border-b border-border">
              <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
                <Sparkles size={16} className="text-violet-600" /> Draft with DRPL
              </h3>
              <button onClick={() => setShowAiModal(false)} className="text-muted-foreground hover:text-muted-foreground">
                <X size={18} />
              </button>
            </div>

            <div className="p-4 space-y-4 flex-1 overflow-y-auto">
              {/* Mode toggle */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1.5">Mode</label>
                <div className="flex gap-2">
                  <button
                    onClick={() => setAiMode('generate')}
                    className={`flex-1 px-3 py-2 rounded-lg text-xs font-medium border transition-colors ${
                      aiMode === 'generate'
                        ? 'border-violet-300 bg-violet-50 text-violet-700'
                        : 'border-border text-muted-foreground hover:bg-muted/40'
                    }`}
                  >
                    Generate New
                  </button>
                  <button
                    onClick={() => setAiMode('enhance')}
                    className={`flex-1 px-3 py-2 rounded-lg text-xs font-medium border transition-colors ${
                      aiMode === 'enhance'
                        ? 'border-violet-300 bg-violet-50 text-violet-700'
                        : 'border-border text-muted-foreground hover:bg-muted/40'
                    }`}
                  >
                    Enhance Existing
                  </button>
                </div>
              </div>

              {/* Prompt */}
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1.5">
                  {aiMode === 'generate'
                    ? 'Describe the document you want to generate'
                    : 'How should the existing content be improved?'}
                </label>
                <textarea
                  value={aiPrompt}
                  onChange={(e) => setAiPrompt(e.target.value)}
                  placeholder={
                    aiMode === 'generate'
                      ? 'e.g. Write a proposal for road construction project in Bihar zone...'
                      : 'e.g. Make the language more formal and add more detail to the methodology section...'
                  }
                  rows={4}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400 resize-y"
                />
              </div>

              {/* Result preview */}
              {aiResult && (
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1.5">Generated Content (Markdown Preview)</label>
                  <div className="border border-border rounded-lg p-3 max-h-[300px] overflow-y-auto bg-muted/40">
                    <pre className="text-xs text-foreground whitespace-pre-wrap font-mono">{aiResult}</pre>
                  </div>
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 p-4 border-t border-border">
              {aiResult ? (
                <>
                  <button
                    onClick={() => setAiResult(null)}
                    className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-muted/40 transition-colors"
                  >
                    Discard
                  </button>
                  <button
                    onClick={applyAiResult}
                    className="px-4 py-2 text-sm bg-violet-600 text-white rounded-lg hover:bg-violet-700 transition-colors font-medium"
                  >
                    Apply to Document
                  </button>
                </>
              ) : (
                <>
                  <button
                    onClick={() => setShowAiModal(false)}
                    className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-muted/40 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleAiGenerate}
                    disabled={aiGenerating || !aiPrompt.trim()}
                    className="flex items-center gap-1.5 px-4 py-2 text-sm bg-gradient-to-r from-violet-600 to-indigo-600 text-white rounded-lg hover:from-violet-700 hover:to-indigo-700 transition-all font-medium disabled:opacity-50"
                  >
                    <Sparkles size={14} />
                    {aiGenerating ? 'Generating...' : 'Generate'}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
