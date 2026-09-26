import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Sparkles, Plus, Upload, Trash2, CheckCircle2, Circle, FileText, Play, Eye, Loader2, AlertCircle, Zap, FileSearch, Paperclip } from 'lucide-react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import {
  getTenderById, getChecklist, getChecklistCompletion, generateChecklist,
  uploadChecklistDocument, deleteChecklistDocument, addChecklistItem,
  generateChecklistDocuments, generateChecklistItemDocument, previewChecklistItemDocument,
} from '../lib/api';
import type { TenderDetail, ChecklistItem, ChecklistCompletion, ChecklistItemCategory } from '../types/tender';

const CATEGORY_CONFIG: Record<ChecklistItemCategory, { label: string; color: string; bg: string; icon: typeof Paperclip }> = {
  standard: { label: 'Standard', color: 'text-muted-foreground', bg: 'bg-muted', icon: Paperclip },
  generated: { label: 'Generated', color: 'text-accent', bg: 'bg-accent/10', icon: Zap },
  analysis: { label: 'Analysis', color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-500/15', icon: FileSearch },
};

const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
  pending: { label: 'Pending', color: 'text-muted-foreground' },
  queued: { label: 'Queued', color: 'text-accent/70' },
  generating: { label: 'Generating...', color: 'text-accent' },
  generated: { label: 'Generated', color: 'text-emerald-600' },
  review: { label: 'In Review', color: 'text-amber-600' },
  approved: { label: 'Approved', color: 'text-emerald-700' },
  failed: { label: 'Failed', color: 'text-red-600' },
};

export default function ChecklistPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const tenderId = Number(id);

  const [tender, setTender] = useState<TenderDetail | null>(null);
  const [items, setItems] = useState<ChecklistItem[]>([]);
  const [completion, setCompletion] = useState<ChecklistCompletion | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [generatingDocs, setGeneratingDocs] = useState(false);
  const [generatingItemId, setGeneratingItemId] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [showAddForm, setShowAddForm] = useState(false);
  const [newItemName, setNewItemName] = useState('');

  const fetchData = useCallback(async () => {
    try {
      const [t, c, comp] = await Promise.all([
        getTenderById(tenderId),
        getChecklist(tenderId),
        getChecklistCompletion(tenderId),
      ]);
      setTender(t);
      setItems(c);
      setCompletion(comp);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load data');
    } finally {
      setLoading(false);
    }
  }, [tenderId]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleGenerate = async () => {
    setGenerating(true);
    try {
      await generateChecklist(tenderId);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to generate checklist');
    } finally {
      setGenerating(false);
    }
  };

  const handleGenerateDocuments = async () => {
    setGeneratingDocs(true);
    try {
      await generateChecklistDocuments(tenderId);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to generate documents');
    } finally {
      setGeneratingDocs(false);
    }
  };

  const handleGenerateItem = async (itemId: number) => {
    setGeneratingItemId(itemId);
    try {
      await generateChecklistItemDocument(tenderId, itemId);
      await fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to generate document');
    } finally {
      setGeneratingItemId(null);
    }
  };

  const handlePreviewItem = async (itemId: number) => {
    try {
      const blob = await previewChecklistItemDocument(tenderId, itemId);
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
    } catch {}
  };

  const handleUpload = async (itemId: number, file: File) => {
    try {
      await uploadChecklistDocument(tenderId, itemId, file);
      await fetchData();
    } catch {}
  };

  const handleDelete = async (itemId: number) => {
    try {
      await deleteChecklistDocument(tenderId, itemId);
      await fetchData();
    } catch {}
  };

  const handleAddItem = async () => {
    if (!newItemName.trim()) return;
    try {
      await addChecklistItem(tenderId, { item_name: newItemName.trim() });
      setNewItemName('');
      setShowAddForm(false);
      await fetchData();
    } catch {}
  };

  if (loading) return <><Header title="Submission checklist" /><LoadingSpinner /></>;

  if (error || !tender) {
    return (
      <>
        <Header title="Document Checklist" />
        <div className="p-6">
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-red-600 dark:bg-red-500/15 dark:border-red-500/20 dark:text-red-400">
            {error || 'Tender not found'}
          </div>
        </div>
      </>
    );
  }

  // Category breakdown stats
  const standardItems = items.filter(i => i.item_category === 'standard');
  const generatedItems = items.filter(i => i.item_category === 'generated');
  const analysisItems = items.filter(i => i.item_category === 'analysis');

  const standardAttached = standardItems.filter(i => i.is_uploaded).length;
  const generatedDone = generatedItems.filter(i => i.generation_status === 'generated' || i.generation_status === 'approved').length;
  const analysisDone = analysisItems.filter(i => i.generation_status === 'generated' || i.generation_status === 'approved').length;

  const hasGeneratable = generatedItems.length > 0 || analysisItems.length > 0;
  const pendingGeneration = items.filter(
    i => i.item_category !== 'standard' && i.generation_status === 'pending'
  ).length;

  const progressPct = completion && completion.total > 0
    ? Math.round((completion.uploaded / completion.total) * 100)
    : 0;

  return (
    <>
      <Header title="Submission checklist" subtitle="See what is required and prepare each document" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">
        <button
          onClick={() => navigate(`/tenders/${tenderId}`)}
          className="flex items-center gap-2 text-sm text-muted-foreground hover:text-drpl-primary transition-colors"
        >
          <ArrowLeft size={16} />
          Back to Tender
        </button>

        {/* Tender header */}
        <div className="bg-card rounded-lg border border-border p-4">
          <h2 className="text-lg font-semibold text-foreground">{tender.title}</h2>
          <p className="text-sm text-muted-foreground font-mono mt-1">
            {tender.portal.toUpperCase()}-{tender.tender_id}
          </p>
        </div>

        {/* Category breakdown progress */}
        {items.length > 0 && (
          <div className="bg-card rounded-lg border border-border p-4 space-y-3">
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-foreground">Document Progress</span>
              <span className="text-sm font-medium text-muted-foreground">{progressPct}%</span>
            </div>
            <div className="w-full bg-muted rounded-full h-2.5">
              <div
                className={`h-2.5 rounded-full transition-all ${
                  completion?.complete ? 'bg-emerald-500' : 'bg-drpl-secondary'
                }`}
                style={{ width: `${progressPct}%` }}
              />
            </div>
            <div className="grid grid-cols-3 gap-3 pt-1">
              <div className="text-center">
                <div className="text-xs text-muted-foreground mb-0.5">Standard</div>
                <div className="text-sm font-semibold text-foreground">
                  {standardAttached}/{standardItems.length}
                  <span className="text-xs font-normal text-muted-foreground ml-1">attached</span>
                </div>
              </div>
              <div className="text-center">
                <div className="text-xs text-accent mb-0.5">Generated</div>
                <div className="text-sm font-semibold text-accent">
                  {generatedDone}/{generatedItems.length}
                  <span className="text-xs font-normal text-accent/70 ml-1">done</span>
                </div>
              </div>
              <div className="text-center">
                <div className="text-xs text-amber-500 mb-0.5">Analysis</div>
                <div className="text-sm font-semibold text-amber-700">
                  {analysisDone}/{analysisItems.length}
                  <span className="text-xs font-normal text-amber-400 ml-1">done</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3 flex-wrap">
          <button
            onClick={handleGenerate}
            disabled={generating}
            className="flex items-center gap-2 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-50"
          >
            <Sparkles size={16} />
            {generating ? 'Preparing...' : items.length > 0 ? 'Refresh checklist' : 'Prepare checklist with DRPL'}
          </button>
          {hasGeneratable && pendingGeneration > 0 && (
            <button
              onClick={handleGenerateDocuments}
              disabled={generatingDocs}
              className="flex items-center gap-2 bg-emerald-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-50"
            >
              <Play size={16} />
              {generatingDocs ? 'Generating Documents...' : `Generate All Documents (${pendingGeneration})`}
            </button>
          )}
          <button
            onClick={() => setShowAddForm(!showAddForm)}
            className="flex items-center gap-2 border border-border text-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-muted/40 transition-colors"
          >
            <Plus size={16} />
            Add Item
          </button>
        </div>

        {/* Add item form */}
        {showAddForm && (
          <div className="bg-card rounded-lg border border-border p-4 flex gap-3">
            <input
              type="text"
              value={newItemName}
              onChange={(e) => setNewItemName(e.target.value)}
              placeholder="Document name..."
              className="flex-1 border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary"
              onKeyDown={(e) => e.key === 'Enter' && handleAddItem()}
            />
            <button
              onClick={handleAddItem}
              className="bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90"
            >
              Add
            </button>
          </div>
        )}

        {/* Checklist items */}
        {items.length === 0 ? (
          <div className="bg-card rounded-lg border border-border p-12 text-center">
            <FileText size={40} className="mx-auto text-muted-foreground/50 mb-3" />
            <p className="text-sm text-muted-foreground mb-2">No checklist items yet</p>
            <p className="text-xs text-muted-foreground">
              Ask DRPL to identify the documents required by this tender.
            </p>
          </div>
        ) : (
          <div className="bg-card rounded-lg border border-border overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="bg-muted/40 border-b border-border">
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase w-8" />
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Document</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Category</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const catConfig = CATEGORY_CONFIG[item.item_category] || CATEGORY_CONFIG.standard;
                  const statusConfig = STATUS_CONFIG[item.generation_status] || STATUS_CONFIG.pending;
                  const CatIcon = catConfig.icon;
                  const isGenerating = generatingItemId === item.id;
                  const canGenerate = item.item_category !== 'standard' && item.generation_status === 'pending';
                  const hasGenerated = item.generation_status === 'generated' || item.generation_status === 'approved';

                  return (
                    <tr key={item.id} className="border-b border-border">
                      <td className="px-4 py-3">
                        {item.is_uploaded || hasGenerated ? (
                          <CheckCircle2 size={18} className="text-emerald-500" />
                        ) : item.generation_status === 'generating' ? (
                          <Loader2 size={18} className="text-accent animate-spin" />
                        ) : item.generation_status === 'failed' ? (
                          <AlertCircle size={18} className="text-red-500" />
                        ) : (
                          <Circle size={18} className="text-muted-foreground/50" />
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <p className="text-sm font-medium text-foreground">{item.item_name}</p>
                        {item.item_description && (
                          <p className="text-xs text-muted-foreground mt-0.5">{item.item_description}</p>
                        )}
                        {item.generation_error && (
                          <p className="text-xs text-red-500 mt-0.5">{item.generation_error}</p>
                        )}
                        {item.source_section && (
                          <p className="text-xs text-muted-foreground mt-0.5">Source: {item.source_section}</p>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded font-medium ${catConfig.bg} ${catConfig.color}`}>
                          <CatIcon size={12} />
                          {catConfig.label}
                        </span>
                        {item.is_required && (
                          <span className="block text-xs text-red-500 mt-1 font-medium">Required</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {item.item_category === 'standard' ? (
                          <span className={`text-xs font-medium ${item.is_uploaded ? 'text-emerald-600' : 'text-muted-foreground'}`}>
                            {item.is_uploaded ? 'Attached' : 'Needs Upload'}
                          </span>
                        ) : (
                          <span className={`text-xs font-medium ${statusConfig.color}`}>
                            {isGenerating ? 'Generating...' : statusConfig.label}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-center gap-2">
                          {/* Standard items: upload/delete */}
                          {item.item_category === 'standard' && (
                            item.is_uploaded ? (
                              <button
                                onClick={() => handleDelete(item.id)}
                                className="text-red-500 hover:text-red-700 transition-colors"
                                title="Remove document"
                              >
                                <Trash2 size={16} />
                              </button>
                            ) : (
                              <label className="cursor-pointer inline-flex items-center gap-1 text-accent hover:text-accent/80 text-sm font-medium transition-colors">
                                <Upload size={14} />
                                Upload
                                <input
                                  type="file"
                                  className="hidden"
                                  onChange={(e) => {
                                    const file = e.target.files?.[0];
                                    if (file) handleUpload(item.id, file);
                                  }}
                                />
                              </label>
                            )
                          )}

                          {/* Generated/Analysis items: generate button */}
                          {canGenerate && (
                            <button
                              onClick={() => handleGenerateItem(item.id)}
                              disabled={isGenerating}
                              className="inline-flex items-center gap-1 text-accent hover:text-accent/80 text-sm font-medium transition-colors disabled:opacity-50"
                              title="Generate this document"
                            >
                              {isGenerating ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                              Generate
                            </button>
                          )}

                          {/* Preview generated document */}
                          {hasGenerated && item.generated_document_id && (
                            <button
                              onClick={() => handlePreviewItem(item.id)}
                              className="inline-flex items-center gap-1 text-emerald-600 hover:text-emerald-800 text-sm font-medium transition-colors"
                              title="Preview generated document"
                            >
                              <Eye size={14} />
                              Preview
                            </button>
                          )}

                          {/* Failed: retry */}
                          {item.generation_status === 'failed' && (
                            <button
                              onClick={() => handleGenerateItem(item.id)}
                              disabled={isGenerating}
                              className="inline-flex items-center gap-1 text-amber-600 hover:text-amber-800 text-sm font-medium transition-colors disabled:opacity-50"
                              title="Retry generation"
                            >
                              {isGenerating ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                              Retry
                            </button>
                          )}

                          {/* Non-standard items can also be manually uploaded */}
                          {item.item_category !== 'standard' && !item.is_uploaded && !hasGenerated && (
                            <label className="cursor-pointer inline-flex items-center gap-1 text-muted-foreground hover:text-muted-foreground text-xs transition-colors" title="Upload manually instead">
                              <Upload size={12} />
                              Manual
                              <input
                                type="file"
                                className="hidden"
                                onChange={(e) => {
                                  const file = e.target.files?.[0];
                                  if (file) handleUpload(item.id, file);
                                }}
                              />
                            </label>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
