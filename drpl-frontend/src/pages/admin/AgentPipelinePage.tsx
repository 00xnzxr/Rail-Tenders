import { useState, useEffect } from 'react';
import {
  Play, RefreshCw, FileSearch, AlertTriangle, Loader2,
  ChevronDown, ChevronRight, CheckCircle2, Clock, XCircle,
  FileText, DollarSign, ClipboardList, Search, Settings2,
  Zap, ArrowRight,
} from 'lucide-react';
import {
  runTenderPipeline, runPipelineStep, getPipelineStatus,
  getLetterheadTemplates, getSignatures, getTenders,
} from '../../lib/api';
import PipelineProgress from '../../components/agents/PipelineProgress';

const PIPELINE_STEPS = [
  {
    key: 'analyze_documents',
    label: 'Document Analysis',
    description: 'Extract requirements, detect negative keywords, flag critical clauses',
    icon: Search,
    color: 'text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-500/15 border-purple-200 dark:border-purple-500/20',
    activeColor: 'bg-purple-600',
  },
  {
    key: 'generate_checklist',
    label: 'Checklist Generation',
    description: 'Generate list of required documents from analysis results',
    icon: ClipboardList,
    color: 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-500/15 border-emerald-200 dark:border-emerald-500/20',
    activeColor: 'bg-emerald-600',
  },
  {
    key: 'generate_documents',
    label: 'Document Generation',
    description: 'Create proposal documents for each checklist item',
    icon: FileText,
    color: 'text-accent bg-accent/10 border-accent/20',
    activeColor: 'bg-accent',
  },
  {
    key: 'research_costing',
    label: 'Costing Research',
    description: 'Research market rates and calculate cost breakdowns with GST',
    icon: DollarSign,
    color: 'text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/15 border-amber-200 dark:border-amber-500/20',
    activeColor: 'bg-amber-600',
  },
];

type StepStatus = 'pending' | 'running' | 'completed' | 'failed';

function getStepStatus(stepKey: string, completedSteps: string[], currentStep?: string, errors?: string[]): StepStatus {
  if (completedSteps.some(s => s.startsWith(stepKey) && s.includes('failed'))) return 'failed';
  if (completedSteps.some(s => s.startsWith(stepKey))) return 'completed';
  if (currentStep === stepKey) return 'running';
  return 'pending';
}

function StepStatusIcon({ status }: { status: StepStatus }) {
  switch (status) {
    case 'completed': return <CheckCircle2 size={18} className="text-emerald-500" />;
    case 'running': return <Loader2 size={18} className="text-accent animate-spin" />;
    case 'failed': return <XCircle size={18} className="text-red-500" />;
    default: return <Clock size={18} className="text-muted-foreground/50" />;
  }
}

export default function AgentPipelinePage() {
  const [tenderId, setTenderId] = useState('');
  const [tenderSearch, setTenderSearch] = useState('');
  const [tenders, setTenders] = useState<any[]>([]);
  const [showTenderDropdown, setShowTenderDropdown] = useState(false);
  const [running, setRunning] = useState(false);
  const [runningStep, setRunningStep] = useState<string | null>(null);
  const [pipelineResult, setPipelineResult] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const [letterheads, setLetterheads] = useState<any[]>([]);
  const [signatures, setSignatures] = useState<any[]>([]);
  const [selectedLetterhead, setSelectedLetterhead] = useState<number | null>(null);
  const [selectedSignatures, setSelectedSignatures] = useState<number[]>([]);
  const [configOpen, setConfigOpen] = useState(true);
  const [expandedStep, setExpandedStep] = useState<string | null>(null);
  const [selectedTender, setSelectedTender] = useState<any>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [lh, sigs] = await Promise.all([getLetterheadTemplates(), getSignatures()]);
        setLetterheads(lh);
        setSignatures(sigs);
      } catch { /* ignore */ }
    };
    load();
  }, []);

  // Search tenders
  useEffect(() => {
    if (tenderSearch.length < 1) { setTenders([]); return; }
    const timer = setTimeout(async () => {
      try {
        const results = (await getTenders({ search: tenderSearch, limit: 10 } as any)).items;
        setTenders(results);
      } catch { setTenders([]); }
    }, 300);
    return () => clearTimeout(timer);
  }, [tenderSearch]);

  const selectTender = (tender: any) => {
    setSelectedTender(tender);
    setTenderId(String(tender.id));
    setTenderSearch('');
    setShowTenderDropdown(false);
  };

  const runPipeline = async () => {
    const id = parseInt(tenderId);
    if (!id || running) return;
    setRunning(true);
    setPipelineResult(null);

    try {
      const result = await runTenderPipeline(id, {
        letterhead_template_id: selectedLetterhead,
        signature_ids: selectedSignatures.length > 0 ? selectedSignatures : undefined,
      });
      setPipelineResult(result);
      setConfigOpen(false);
    } catch (err: any) {
      setPipelineResult({ status: 'failed', error: err.response?.data?.detail || err.message });
    } finally {
      setRunning(false);
    }
  };

  const runSingleStep = async (step: string) => {
    const id = parseInt(tenderId);
    if (!id || running || runningStep) return;
    setRunningStep(step);

    try {
      const result = await runPipelineStep(id, step, {
        letterhead_template_id: selectedLetterhead,
        signature_ids: selectedSignatures.length > 0 ? selectedSignatures : undefined,
      });
      // Merge into pipeline result
      setPipelineResult((prev: any) => {
        const merged = { ...prev };
        if (step === 'analyze_documents') {
          merged.analysis_result = result.analysis || result;
          merged.negative_keywords = result.analysis?.negative_keywords || result.negative_keywords || [];
        } else if (step === 'generate_checklist') {
          merged.checklist_items = result.items || [];
        } else if (step === 'generate_documents') {
          merged.generated_documents = result.generated_documents || result;
        } else if (step === 'research_costing') {
          merged.costing_data = result.costing || result;
        }
        merged.completed_steps = [...(merged.completed_steps || []), step];
        return merged;
      });
      setExpandedStep(step);
    } catch (err: any) {
      setPipelineResult((prev: any) => ({
        ...prev,
        errors: [...(prev?.errors || []), `${step}: ${err.response?.data?.detail || err.message}`],
        completed_steps: [...(prev?.completed_steps || []), `${step} (failed)`],
      }));
    } finally {
      setRunningStep(null);
    }
  };

  const checkStatus = async () => {
    const id = parseInt(tenderId);
    if (!id) return;
    try {
      const s = await getPipelineStatus(id);
      setStatus(s);
    } catch { /* ignore */ }
  };

  const completedSteps = pipelineResult?.completed_steps || [];
  const errors = pipelineResult?.errors || [];

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">AI Tender Pipeline</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Run the full LangChain 4-step pipeline or execute individual steps
        </p>
      </div>

      {/* Tender Selector + Config */}
      <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
        <button
          onClick={() => setConfigOpen(!configOpen)}
          className="w-full flex items-center justify-between px-6 py-4 hover:bg-muted/40 transition-colors"
        >
          <div className="flex items-center gap-3">
            <Settings2 size={18} className="text-muted-foreground" />
            <span className="font-semibold text-foreground">Pipeline Configuration</span>
            {selectedTender && (
              <span className="text-xs bg-accent/10 text-accent px-2 py-0.5 rounded-full font-medium">
                Tender #{selectedTender.id}: {selectedTender.title?.substring(0, 40)}...
              </span>
            )}
          </div>
          {configOpen ? <ChevronDown size={18} className="text-muted-foreground" /> : <ChevronRight size={18} className="text-muted-foreground" />}
        </button>

        {configOpen && (
          <div className="px-6 pb-6 border-t border-border pt-4">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {/* Tender Selector */}
              <div className="relative">
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Tender</label>
                <div className="relative">
                  <input
                    type="text"
                    value={tenderSearch || (selectedTender ? `#${selectedTender.id} - ${selectedTender.title?.substring(0, 30)}` : tenderId)}
                    onChange={e => {
                      setTenderSearch(e.target.value);
                      setShowTenderDropdown(true);
                      if (/^\d+$/.test(e.target.value)) setTenderId(e.target.value);
                    }}
                    onFocus={() => { if (tenderSearch) setShowTenderDropdown(true); }}
                    placeholder="Search or enter tender ID..."
                    className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring focus:border-ring"
                  />
                  <Search size={14} className="absolute right-3 top-3 text-muted-foreground" />
                </div>
                {showTenderDropdown && tenders.length > 0 && (
                  <div className="absolute z-20 mt-1 w-full bg-card border border-border rounded-lg shadow-lg max-h-48 overflow-y-auto">
                    {tenders.map(t => (
                      <button
                        key={t.id}
                        onClick={() => selectTender(t)}
                        className="w-full text-left px-3 py-2 hover:bg-accent/10 text-sm border-b border-border last:border-0"
                      >
                        <span className="font-medium text-foreground">#{t.id}</span>
                        <span className="text-muted-foreground ml-2">{t.title?.substring(0, 50)}</span>
                        {t.portal && <span className="ml-2 text-[10px] uppercase text-muted-foreground bg-muted px-1.5 py-0.5 rounded">{t.portal}</span>}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Letterhead */}
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Letterhead</label>
                <select
                  value={selectedLetterhead || ''}
                  onChange={e => setSelectedLetterhead(e.target.value ? parseInt(e.target.value) : null)}
                  className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring"
                >
                  <option value="">No letterhead</option>
                  {letterheads.map(lh => (
                    <option key={lh.id} value={lh.id}>{lh.name}</option>
                  ))}
                </select>
              </div>

              {/* Signatures */}
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Signatures</label>
                <div className="flex flex-wrap gap-2 mt-1">
                  {signatures.map(sig => (
                    <label key={sig.id} className="flex items-center gap-1.5 text-sm cursor-pointer bg-muted/40 hover:bg-muted px-2.5 py-1.5 rounded-lg border border-border transition-colors">
                      <input
                        type="checkbox"
                        checked={selectedSignatures.includes(sig.id)}
                        onChange={e => {
                          if (e.target.checked) setSelectedSignatures(p => [...p, sig.id]);
                          else setSelectedSignatures(p => p.filter(id => id !== sig.id));
                        }}
                        className="rounded border-border text-accent"
                      />
                      <span className="text-muted-foreground">{sig.name}</span>
                    </label>
                  ))}
                  {signatures.length === 0 && <span className="text-xs text-muted-foreground py-1">No signatures configured</span>}
                </div>
              </div>
            </div>

            {/* Action Buttons */}
            <div className="flex items-center gap-3 mt-5 pt-4 border-t border-border">
              <button
                onClick={runPipeline}
                disabled={!tenderId || running}
                className="flex items-center gap-2 px-5 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed font-medium text-sm transition-colors shadow-sm"
              >
                {running ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
                {running ? 'Running Pipeline...' : 'Run Full Pipeline'}
              </button>
              <button
                onClick={checkStatus}
                disabled={!tenderId}
                className="flex items-center gap-2 px-4 py-2.5 border border-border rounded-lg hover:bg-muted/40 text-sm font-medium text-muted-foreground transition-colors"
              >
                <RefreshCw size={16} />
                Check Status
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Pipeline Steps (Vertical Timeline) */}
      <div className="flex gap-6">
        {/* Steps Column */}
        <div className="w-80 shrink-0 space-y-0">
          {PIPELINE_STEPS.map((step, idx) => {
            const stepStatus = getStepStatus(step.key, completedSteps, runningStep || undefined, errors);
            const Icon = step.icon;
            const isExpanded = expandedStep === step.key;

            return (
              <div key={step.key} className="relative">
                {/* Connector Line */}
                {idx < PIPELINE_STEPS.length - 1 && (
                  <div className="absolute left-[22px] top-[52px] bottom-0 w-0.5 bg-muted">
                    {stepStatus === 'completed' && <div className="w-full h-full bg-emerald-300" />}
                  </div>
                )}

                <div
                  className={`relative flex items-start gap-3 p-4 rounded-xl border transition-all cursor-pointer ${
                    isExpanded
                      ? 'border-accent/20 bg-accent/5 shadow-sm'
                      : 'border-transparent hover:bg-muted/40'
                  }`}
                  onClick={() => setExpandedStep(isExpanded ? null : step.key)}
                >
                  {/* Status Icon */}
                  <div className="shrink-0 mt-0.5">
                    <StepStatusIcon status={stepStatus} />
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <Icon size={14} className="text-muted-foreground" />
                      <span className="text-sm font-semibold text-foreground">{step.label}</span>
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5 leading-relaxed">{step.description}</p>

                    {/* Run individual step */}
                    {tenderId && stepStatus === 'pending' && (
                      <button
                        onClick={(e) => { e.stopPropagation(); runSingleStep(step.key); }}
                        disabled={!!runningStep}
                        className="flex items-center gap-1 mt-2 text-[11px] font-medium text-accent hover:text-accent/80 disabled:opacity-40"
                      >
                        <Play size={10} /> Run Step
                      </button>
                    )}

                    {runningStep === step.key && (
                      <div className="flex items-center gap-1.5 mt-2 text-[11px] text-accent">
                        <Loader2 size={10} className="animate-spin" /> Running...
                      </div>
                    )}
                  </div>

                  {isExpanded ? <ChevronDown size={16} className="text-muted-foreground shrink-0" /> : <ChevronRight size={16} className="text-muted-foreground shrink-0" />}
                </div>
              </div>
            );
          })}
        </div>

        {/* Results Panel */}
        <div className="flex-1 min-w-0">
          {!pipelineResult && !status && (
            <div className="bg-card rounded-xl border border-border shadow-sm p-12 text-center">
              <Zap size={40} className="text-muted-foreground/40 mx-auto mb-3" />
              <h3 className="text-sm font-semibold text-muted-foreground">No Results Yet</h3>
              <p className="text-xs text-muted-foreground mt-1">
                Select a tender and run the pipeline or individual steps to see results here.
              </p>
            </div>
          )}

          {/* Pipeline Progress Bar */}
          {pipelineResult && (
            <div className="bg-card rounded-xl border border-border shadow-sm p-6 mb-4">
              <PipelineProgress
                completedSteps={completedSteps}
                currentStep={running ? pipelineResult.current_step : undefined}
                errors={errors}
              />
            </div>
          )}

          {/* Expanded Step Results */}
          {expandedStep && pipelineResult && (
            <div className="bg-card rounded-xl border border-border shadow-sm p-6 space-y-4">
              {expandedStep === 'analyze_documents' && pipelineResult.analysis_result && (
                <>
                  <h3 className="font-semibold text-foreground flex items-center gap-2">
                    <Search size={16} className="text-purple-600 dark:text-purple-400" /> Document Analysis Results
                  </h3>
                  {pipelineResult.analysis_result.summary && (
                    <div className="bg-accent/10 border border-accent/20 rounded-lg p-4">
                      <p className="text-sm text-accent">{pipelineResult.analysis_result.summary}</p>
                    </div>
                  )}
                  {pipelineResult.negative_keywords?.length > 0 && (
                    <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-4">
                      <h4 className="font-medium text-red-700 dark:text-red-400 flex items-center gap-2 mb-2 text-sm">
                        <AlertTriangle size={14} /> Rejection Risks ({pipelineResult.negative_keywords.length})
                      </h4>
                      <div className="space-y-2 max-h-60 overflow-y-auto">
                        {pipelineResult.negative_keywords.map((kw: any, i: number) => (
                          <div key={i} className="flex items-start gap-2">
                            <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${
                              kw.severity === 'critical' ? 'bg-red-200 text-red-800 dark:text-red-400' :
                              kw.severity === 'high' ? 'bg-orange-200 text-orange-800 dark:text-orange-400' : 'bg-yellow-200 text-yellow-800 dark:text-yellow-400'
                            }`}>{kw.severity}</span>
                            <p className="text-xs text-foreground flex-1">{kw.sentence || kw.text || JSON.stringify(kw)}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {/* Requirements categories */}
                  {pipelineResult.analysis_result.requirements && (
                    <div className="space-y-2">
                      {Object.entries(pipelineResult.analysis_result.requirements as Record<string, any[]>).map(([cat, items]) => {
                        if (!Array.isArray(items) || items.length === 0) return null;
                        return (
                          <div key={cat} className="border border-border rounded-lg p-3">
                            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">
                              {cat.replace(/_/g, ' ')} ({items.length})
                            </h4>
                            <div className="space-y-1 max-h-32 overflow-y-auto">
                              {items.slice(0, 8).map((item: any, i: number) => (
                                <p key={i} className="text-xs text-muted-foreground">
                                  {typeof item === 'string' ? item : item.description || item.text || JSON.stringify(item)}
                                </p>
                              ))}
                              {items.length > 8 && <p className="text-xs text-muted-foreground italic">...and {items.length - 8} more</p>}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              )}

              {expandedStep === 'generate_checklist' && pipelineResult.checklist_items?.length > 0 && (
                <>
                  <h3 className="font-semibold text-foreground flex items-center gap-2">
                    <ClipboardList size={16} className="text-emerald-600 dark:text-emerald-400" /> Checklist Items ({pipelineResult.checklist_items.length})
                  </h3>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-border">
                          <th className="text-left pb-2 text-xs font-semibold text-muted-foreground uppercase">#</th>
                          <th className="text-left pb-2 text-xs font-semibold text-muted-foreground uppercase">Document</th>
                          <th className="text-left pb-2 text-xs font-semibold text-muted-foreground uppercase">Required</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {pipelineResult.checklist_items.map((item: any, i: number) => (
                          <tr key={i}>
                            <td className="py-2 text-muted-foreground">{i + 1}</td>
                            <td className="py-2 text-foreground">{item.name}</td>
                            <td className="py-2">
                              {item.is_required !== false ? (
                                <span className="text-emerald-600 dark:text-emerald-400 text-xs font-medium">Required</span>
                              ) : (
                                <span className="text-muted-foreground text-xs">Optional</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}

              {expandedStep === 'generate_documents' && pipelineResult.generated_documents?.length > 0 && (
                <>
                  <h3 className="font-semibold text-foreground flex items-center gap-2">
                    <FileText size={16} className="text-accent" /> Generated Documents ({pipelineResult.generated_documents.length})
                  </h3>
                  <div className="grid grid-cols-1 gap-3">
                    {pipelineResult.generated_documents.map((doc: any, i: number) => (
                      <div key={i} className={`border rounded-lg p-3 ${doc.status === 'generated' ? 'border-emerald-200 dark:border-emerald-500/20 bg-emerald-50/50' : 'border-red-200 dark:border-red-500/20 bg-red-50/50'}`}>
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-medium text-foreground">{doc.checklist_item}</span>
                          <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                            doc.status === 'generated' ? 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400' : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'
                          }`}>{doc.status}</span>
                        </div>
                        <span className="text-xs text-muted-foreground">{doc.document_type}</span>
                        {doc.content_preview && <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{doc.content_preview}</p>}
                      </div>
                    ))}
                  </div>
                </>
              )}

              {expandedStep === 'research_costing' && pipelineResult.costing_data && (
                <>
                  <h3 className="font-semibold text-foreground flex items-center gap-2">
                    <DollarSign size={16} className="text-amber-600 dark:text-amber-400" /> Costing Results
                  </h3>
                  <pre className="bg-muted/40 border border-border rounded-lg p-4 text-xs text-foreground overflow-x-auto max-h-80">
                    {JSON.stringify(pipelineResult.costing_data, null, 2)}
                  </pre>
                </>
              )}

              {/* No results for this step yet */}
              {expandedStep && !pipelineResult[expandedStep === 'analyze_documents' ? 'analysis_result' : expandedStep === 'generate_checklist' ? 'checklist_items' : expandedStep === 'generate_documents' ? 'generated_documents' : 'costing_data'] && (
                <div className="text-center py-8 text-muted-foreground">
                  <Clock size={24} className="mx-auto mb-2 opacity-40" />
                  <p className="text-sm">No results for this step yet. Run it to see output.</p>
                </div>
              )}
            </div>
          )}

          {/* Status Check */}
          {status && (
            <div className="bg-card rounded-xl border border-border shadow-sm p-6 mt-4">
              <h2 className="font-semibold text-foreground mb-3">Latest Pipeline Status</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-muted/40 rounded-lg p-3">
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold">Status</span>
                  <p className={`text-sm font-bold mt-0.5 ${status.status === 'completed' ? 'text-emerald-600 dark:text-emerald-400' : status.status === 'failed' ? 'text-red-600 dark:text-red-400' : 'text-accent'}`}>
                    {status.status}
                  </p>
                </div>
                <div className="bg-muted/40 rounded-lg p-3">
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold">Duration</span>
                  <p className="text-sm font-bold mt-0.5 text-foreground">{status.latency_ms ? `${(status.latency_ms / 1000).toFixed(1)}s` : 'N/A'}</p>
                </div>
                <div className="bg-muted/40 rounded-lg p-3">
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold">Execution</span>
                  <p className="text-sm font-bold mt-0.5 text-foreground">#{status.execution_id || 'N/A'}</p>
                </div>
                <div className="bg-muted/40 rounded-lg p-3">
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold">Created</span>
                  <p className="text-sm font-bold mt-0.5 text-foreground">{status.created_at ? new Date(status.created_at).toLocaleString() : 'N/A'}</p>
                </div>
              </div>
              {status.error && (
                <p className="mt-3 text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-3">{status.error}</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
