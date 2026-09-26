import { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import {
  Save, Layers, ChevronRight, Brain, RefreshCw, Wrench as WrenchIcon,
  GitBranch, Settings2, Play, Send, RotateCcw, Code, Zap,
  Activity, Clock, Coins, Hash, X, Plus, Info, Upload, FileText, Trash2,
  CheckCircle2, XCircle, Loader2, BarChart3,
  Sparkles, AlertTriangle, Undo2,
} from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import ToolConfigPanel from '../../components/agents/ToolConfigPanel';
import WorkflowEditor from '../../components/agents/WorkflowEditor';
import ExecutionTraceViewer from '../../components/agents/ExecutionTraceViewer';
import ToolCallTrace from '../../components/agents/ToolCallTrace';
import {
  getBuilderAgent, createBuilderAgent, updateBuilderAgent,
  executeBuilderAgent, executeBuilderAgentWithFiles, getAgentVersions, rollbackAgent,
  publishBuilderAgent, getBuilderAgents, getBuilderTools,
  getAgentStats, getAgentExecutions, getAgentExecutionDetail,
  getAgentTests, createAgentTest, deleteAgentTest, runAgentTest, runAllAgentTests,
  uploadTestDocuments, getTestDocuments, deleteTestDocument, deleteAllTestDocuments,
  reExtractTestDocument,
  getTrainingDatasets, getAgentTrainingDatasets, assignDatasetToAgent,
  unassignDatasetFromAgent, getMCPServers,
  // Phase 3d — canonical / reset / validate
  getAgentCanonical, validateAgentPrompt, resetAgentToDefault,
  type CanonicalAgentResponse, type PromptValidationResponse,
  getModelCatalog,
  getAgentEffectiveTools,
} from '../../lib/api';
import { formatDateTime } from '../../lib/formatters';

/**
 * The model catalog is served by the backend (`GET /api/agent-builder/models`).
 *
 * It used to be four hardcoded arrays in this file — the model list, an unused
 * OpenAI list, the provider list, and a copy of the per-model output ceilings
 * with a comment asking the next person to keep it in sync with the backend.
 * They drifted, and the drift was invisible rather than loud: an agent saved as
 * `claude-opus-5` rendered as `claude-sonnet-4-6` purely because the new ID was
 * missing from the array, and the Max Tokens hint showed an 8,192 fallback for
 * a model that supports 128,000.
 */
export interface ModelInfo {
  id: string;
  provider: string;
  tier: string | null;
  is_current: boolean;
  max_output_tokens: number;
  input_price_per_mtok: number;
  output_price_per_mtok: number;
  supports_adaptive_thinking: boolean;
  supports_thinking_budget: boolean;
  supports_effort: boolean;
  supports_max_effort: boolean;
  supports_sampling: boolean;
}

interface ModelCatalog {
  models: ModelInfo[];
  providers: string[];
  effort_levels: string[];
  default_max_output_tokens: number;
}

// Used only until the catalog loads, and if the request fails. Deliberately
// minimal: a stale long list is what caused the original problem.
const EMPTY_CATALOG: ModelCatalog = {
  models: [],
  providers: ['anthropic', 'openai', 'google'],
  effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'],
  default_max_output_tokens: 8_192,
};

const EFFORT_LABELS: Record<string, string> = {
  low: 'Low (fast, minimal thinking)',
  medium: 'Medium (balanced)',
  high: 'High (deep reasoning)',
  xhigh: 'Extra high (best for agentic work)',
  max: 'Max (correctness over cost)',
};

const CATEGORY_OPTIONS = ['general', 'analysis', 'generation', 'extraction', 'orchestration'];
const PROMPT_VARIABLES = ['{tender_id}', '{company_name}', '{query}', '{document_text}', '{checklist_items}', '{context}'];

const MIN_MAX_TOKENS = 512;

type TabKey = 'config' | 'tools' | 'workflow' | 'training' | 'test' | 'monitoring';

const TABS: { key: TabKey; label: string; icon: any }[] = [
  { key: 'config', label: 'Configuration', icon: Settings2 },
  { key: 'tools', label: 'Tools & Capabilities', icon: WrenchIcon },
  { key: 'workflow', label: 'Workflow', icon: GitBranch },
  { key: 'training', label: 'Knowledge Sources', icon: Layers },
  { key: 'test', label: 'Test & Debug', icon: Play },
  { key: 'monitoring', label: 'Monitoring', icon: Activity },
];

const EXECUTION_MODES = [
  {
    type: 'chain_of_thought', label: 'Simple', icon: Brain,
    desc: 'Single LLM call with no tool usage. Input is processed through the system prompt and model, producing a direct output.',
    color: 'purple',
  },
  {
    type: 'react', label: 'ReAct Agent', icon: RefreshCw,
    desc: 'Iterative think-act-observe loop with real tool calling via LangChain. The agent reasons, selects tools, and iterates until reaching an answer.',
    color: 'blue',
  },
  {
    type: 'orchestrator', label: 'Orchestrator', icon: GitBranch,
    desc: 'Coordinates multiple agents in a workflow graph. Define steps, conditions, and data flow between agents.',
    color: 'amber',
  },
];

function generateKey(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

export default function AgentEditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const isNew = !id || id === 'new';

  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>('config');
  const [catalog, setCatalog] = useState<ModelCatalog>(EMPTY_CATALOG);
  const [effectiveTools, setEffectiveTools] = useState<any | null>(null);

  // Form state
  const [form, setForm] = useState({
    display_name: '', agent_key: '', description: '', category: 'general',
    tags: [] as string[], system_prompt: '',
    model: '', provider: 'anthropic',
    temperature: 0.7, max_tokens: 4096,
    agent_type: 'chain_of_thought',
    thinking_mode: '' as string,          // "", "auto", "adaptive", "enabled", "disabled"
    thinking_budget_tokens: 10000,
    effort: '' as string,                 // "", "low", "medium", "high", "max"
    tools: [] as Array<{ tool_id: number; config: Record<string, any> }>,
    mcp_servers: [] as string[],
    learning_enabled: true,
    input_schema: '{}', output_schema: '{}',
    enabled: true, orchestration_config: null as any,
    langchain_config: { max_iterations: 10, return_intermediate_steps: true } as any,
  });

  const [tagInput, setTagInput] = useState('');
  const [isSystem, setIsSystem] = useState(false);

  // Phase 3d — canonical registry state (Customized badge, Reset, validation)
  const [isUserCustomized, setIsUserCustomized] = useState(false);
  const [canonical, setCanonical] = useState<CanonicalAgentResponse | null>(null);
  const [validation, setValidation] = useState<PromptValidationResponse | null>(null);
  const [resetting, setResetting] = useState(false);
  const validationDebounceRef = useRef<number | null>(null);

  // Test state
  const [testInput, setTestInput] = useState('');
  const [testOutput, setTestOutput] = useState<any>(null);
  const [testRunning, setTestRunning] = useState(false);
  const [testCases, setTestCases] = useState<any[]>([]);
  const [testResults, setTestResults] = useState<Record<number, any>>({});
  const [runningAllTests, setRunningAllTests] = useState(false);
  const [testFiles, setTestFiles] = useState<File[]>([]);
  const [storedDocs, setStoredDocs] = useState<any[]>([]);
  const [uploadingDocs, setUploadingDocs] = useState(false);

  // Monitoring state
  const [stats, setStats] = useState<any>(null);
  const [executions, setExecutions] = useState<any[]>([]);
  const [expandedExec, setExpandedExec] = useState<number | null>(null);
  const [expandedExecDetail, setExpandedExecDetail] = useState<any>(null);
  const [monitorSubTab, setMonitorSubTab] = useState<'executions' | 'versions'>('executions');

  // Versions state
  const [versions, setVersions] = useState<any[]>([]);
  const [diffVersions, setDiffVersions] = useState<[any, any] | null>(null);

  // Training datasets state
  const [allDatasets, setAllDatasets] = useState<any[]>([]);
  const [assignedDatasets, setAssignedDatasets] = useState<any[]>([]);
  const [loadingDatasets, setLoadingDatasets] = useState(false);
  const [assigningDataset, setAssigningDataset] = useState<number | null>(null);

  // MCP servers state
  const [mcpServers, setMcpServers] = useState<any[]>([]);

  // Available agents for orchestrator workflow
  const [availableAgents, setAvailableAgents] = useState<any[]>([]);
  const [availableToolsList, setAvailableToolsList] = useState<any[]>([]);

  // One fetch, before anything renders a model name. On failure we keep the
  // empty catalog and still show the agent's saved model, rather than
  // substituting something it is not.
  useEffect(() => {
    getModelCatalog()
      .then(setCatalog)
      .catch(() => setCatalog(EMPTY_CATALOG));
  }, []);

  // What the agent will actually run with, which is not always the stored
  // assignment. Refetched when the id changes; a failure just hides the banner
  // rather than blocking the tab.
  useEffect(() => {
    if (!id || id === 'new') {
      setEffectiveTools(null);
      return;
    }
    getAgentEffectiveTools(Number(id))
      .then(setEffectiveTools)
      .catch(() => setEffectiveTools(null));
  }, [id]);

  const isMasterAgent = form.agent_key === 'decision_maker';

  const providerModels = catalog.models.filter(m => m.provider === form.provider);
  const selectedModel = catalog.models.find(m => m.id === form.model);
  const modelMaxTokens = selectedModel?.max_output_tokens ?? catalog.default_max_output_tokens;

  useEffect(() => {
    const init = async () => {
      try {
        const [agents, tools, servers] = await Promise.all([
          getBuilderAgents({}).catch(() => []),
          getBuilderTools().catch(() => []),
          getMCPServers().catch(() => []),
        ]);
        setAvailableAgents(agents);
        setAvailableToolsList(tools);
        setMcpServers(servers);

        if (!isNew && id) {
          const agent = await getBuilderAgent(Number(id));
          setIsSystem(agent.is_system || false);
          setIsUserCustomized(agent.is_user_customized || false);
          // Fetch canonical info for system agents — powers Customized badge,
          // Reset button, placeholder validator. Silently skip if not registered.
          getAgentCanonical(Number(id))
            .then((c) => {
              setCanonical(c);
              if (c.registered && agent.system_prompt) {
                // Initial validation against the just-loaded prompt.
                validateAgentPrompt(Number(id), agent.system_prompt)
                  .then(setValidation)
                  .catch(() => {});
              }
            })
            .catch(() => setCanonical(null));
          setForm({
            display_name: agent.display_name || '',
            agent_key: agent.agent_key || '',
            description: agent.description || '',
            category: agent.category || 'general',
            tags: agent.tags || [],
            system_prompt: agent.system_prompt || '',
            model: agent.model || '',
            provider: agent.provider || 'anthropic',
            temperature: agent.temperature ?? 0.7,
            max_tokens: agent.max_tokens || 4096,
            agent_type: agent.agent_type || 'chain_of_thought',
            thinking_mode: agent.thinking_mode || '',
            thinking_budget_tokens: agent.thinking_budget_tokens || 10000,
            effort: agent.effort || '',
            tools: agent.tools || [],
            mcp_servers: agent.mcp_servers || [],
            learning_enabled: agent.learning_enabled ?? true,
            input_schema: JSON.stringify(agent.input_schema || {}, null, 2),
            output_schema: JSON.stringify(agent.output_schema || {}, null, 2),
            enabled: agent.is_enabled ?? true,
            orchestration_config: agent.orchestration_config || null,
            langchain_config: agent.langchain_config || { max_iterations: 10, return_intermediate_steps: true },
          });
        }
      } catch { /* ignore */ }
      finally { setLoading(false); }
    };
    init();
  }, [id, isNew]);

  // Load tab-specific data
  useEffect(() => {
    if (isNew || !id) return;
    const agentId = Number(id);
    if (activeTab === 'test') {
      getAgentTests(agentId).then(setTestCases).catch(() => {});
      getTestDocuments(agentId).then(setStoredDocs).catch(() => {});
    } else if (activeTab === 'training') {
      setLoadingDatasets(true);
      Promise.all([
        getTrainingDatasets('active').catch(() => []),
        getAgentTrainingDatasets(agentId).catch(() => []),
      ]).then(([all, assigned]) => {
        setAllDatasets(all);
        setAssignedDatasets(assigned);
      }).finally(() => setLoadingDatasets(false));
    } else if (activeTab === 'monitoring') {
      Promise.all([
        getAgentStats(agentId).catch(() => null),
        getAgentExecutions(agentId).catch(() => []),
        getAgentVersions(agentId).catch(() => []),
      ]).then(([s, e, v]) => {
        setStats(s);
        setExecutions(e);
        setVersions(v);
      });
    }
  }, [activeTab, id, isNew]);

  const handleNameChange = (name: string) => {
    setForm(f => ({ ...f, display_name: name, agent_key: isNew ? generateKey(name) : f.agent_key }));
  };

  const addTag = () => {
    const t = tagInput.trim();
    if (t && !form.tags.includes(t)) {
      setForm(f => ({ ...f, tags: [...f.tags, t] }));
      setTagInput('');
    }
  };

  const removeTag = (tag: string) => {
    setForm(f => ({ ...f, tags: f.tags.filter(t => t !== tag) }));
  };

  const insertVariable = (v: string) => {
    setForm(f => ({ ...f, system_prompt: f.system_prompt + v }));
  };

  // Phase 3d — debounced live validation as the user edits the prompt.
  const handlePromptChange = (newPrompt: string) => {
    setForm(f => ({ ...f, system_prompt: newPrompt }));
    if (!canonical?.registered || isNew || !id) return;
    if (validationDebounceRef.current) {
      window.clearTimeout(validationDebounceRef.current);
    }
    validationDebounceRef.current = window.setTimeout(() => {
      validateAgentPrompt(Number(id), newPrompt)
        .then(setValidation)
        .catch(() => {});
    }, 250);
  };

  // Phase 3d — reset prompt + tools to canonical (with confirm).
  const handleResetToDefault = async () => {
    if (!id || isNew) return;
    if (!window.confirm(
      `Reset ${form.display_name || 'this agent'} to system default?\n\n` +
      `Your current system_prompt and tools will be saved as version ${form ? '+1' : 'next'} ` +
      `so you can roll back from the Monitoring → Versions tab. Continue?`,
    )) return;
    setResetting(true);
    try {
      await resetAgentToDefault(Number(id), ['system_prompt', 'tools']);
      // Reload the agent to reflect canonical values.
      const agent = await getBuilderAgent(Number(id));
      setIsUserCustomized(agent.is_user_customized || false);
      setForm(f => ({
        ...f,
        system_prompt: agent.system_prompt || '',
        tools: agent.tools || [],
      }));
      const c = await getAgentCanonical(Number(id));
      setCanonical(c);
      if (c.registered) {
        const v = await validateAgentPrompt(Number(id), agent.system_prompt || '');
        setValidation(v);
      }
    } catch (err: any) {
      alert(err?.response?.data?.detail || err?.message || 'Reset failed');
    } finally {
      setResetting(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const body: Record<string, any> = {
        display_name: form.display_name, agent_key: form.agent_key,
        description: form.description, category: form.category,
        tags: form.tags, system_prompt: form.system_prompt,
        model: form.model, provider: form.provider,
        temperature: form.temperature, max_tokens: form.max_tokens,
        agent_type: form.agent_type, tools: form.tools,
        mcp_servers: form.mcp_servers,
        learning_enabled: form.learning_enabled,
        thinking_mode: form.thinking_mode || null,
        thinking_budget_tokens: form.thinking_mode === 'enabled' ? form.thinking_budget_tokens : null,
        effort: form.effort || null,
        enabled: form.enabled, orchestration_config: form.orchestration_config,
        langchain_config: form.langchain_config,
      };
      try { body.input_schema = JSON.parse(form.input_schema); } catch { body.input_schema = {}; }
      try { body.output_schema = JSON.parse(form.output_schema); } catch { body.output_schema = {}; }

      if (isNew) {
        const created = await createBuilderAgent(body);
        navigate(`/admin/agent-builder/${created.id}`, { replace: true });
      } else {
        const result = await updateBuilderAgent(Number(id), body);
        // Phase 3d — refresh customized flag + canonical view after a save.
        if (typeof result?.is_user_customized === 'boolean') {
          setIsUserCustomized(result.is_user_customized);
        }
        if (canonical?.registered) {
          getAgentCanonical(Number(id))
            .then(setCanonical)
            .catch(() => {});
        }
        if (result?.warnings && result.warnings.length > 0) {
          alert(`Saved with warnings:\n\n${result.warnings.join('\n')}`);
        }
      }
    } catch (err: any) {
      // Phase 3d — surface validation 400s from the API.
      const detail = err?.response?.data?.detail;
      if (detail && typeof detail === 'object' && detail.validation) {
        const missing = (detail.validation.missing_placeholders || []).join(', ');
        alert(
          `Cannot save: ${detail.message}\n\nMissing required placeholders: ${missing}`,
        );
      } else if (typeof detail === 'string') {
        alert(`Save failed: ${detail}`);
      }
    }
    finally { setSaving(false); }
  };

  const handlePublish = async () => {
    if (!id || isNew) return;
    try { await publishBuilderAgent(Number(id)); alert('Agent published successfully.'); } catch { /* ignore */ }
  };

  const handleUploadDocuments = async (files: FileList | File[]) => {
    if (!id || isNew) return;
    const fileArray = Array.from(files);
    if (fileArray.length === 0) return;
    setUploadingDocs(true);
    try {
      const result = await uploadTestDocuments(Number(id), fileArray);
      // Refresh stored docs list
      const docs = await getTestDocuments(Number(id));
      setStoredDocs(docs);
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Upload failed');
    }
    setUploadingDocs(false);
  };

  const handleDeleteDoc = async (docId: number) => {
    try {
      await deleteTestDocument(docId);
      setStoredDocs(prev => prev.filter(d => d.id !== docId));
    } catch { /* ignore */ }
  };

  const handleDeleteAllDocs = async () => {
    if (!id || isNew) return;
    try {
      await deleteAllTestDocuments(Number(id));
      setStoredDocs([]);
    } catch { /* ignore */ }
  };

  const handleReExtract = async (docId: number) => {
    try {
      const result = await reExtractTestDocument(docId);
      setStoredDocs(prev => prev.map(d => d.id === docId ? { ...d, ...result } : d));
    } catch { /* ignore */ }
  };

  const handleTest = async () => {
    if (!id || isNew) return;
    setTestRunning(true);
    setTestOutput(null);
    try {
      let inputData: any = {};
      try { inputData = JSON.parse(testInput); } catch { inputData = { input: testInput }; }

      const hasNewFiles = testFiles.length > 0;
      const hasStoredDocs = storedDocs.length > 0;

      let result;
      if (hasNewFiles || hasStoredDocs) {
        const docIds = storedDocs.map(d => d.id);
        result = await executeBuilderAgentWithFiles(Number(id), inputData, testFiles, docIds);
        // If new files were uploaded during execution, refresh the stored docs list
        if (result.new_document_ids?.length > 0) {
          setTestFiles([]);
          const docs = await getTestDocuments(Number(id));
          setStoredDocs(docs);
        }
      } else {
        result = await executeBuilderAgent(Number(id), inputData);
      }
      setTestOutput(result);
    } catch (err: any) {
      setTestOutput({ error: err?.response?.data?.detail || 'Execution failed' });
    }
    setTestRunning(false);
  };

  const handleRunAllTests = async () => {
    if (!id || isNew) return;
    setRunningAllTests(true);
    try {
      const results = await runAllAgentTests(Number(id));
      const map: Record<number, any> = {};
      results.forEach((r: any) => { map[r.test_case_id] = r; });
      setTestResults(map);
    } catch { /* ignore */ }
    setRunningAllTests(false);
  };

  const handleRunSingleTest = async (testId: number) => {
    try {
      const result = await runAgentTest(testId);
      setTestResults(prev => ({ ...prev, [testId]: result }));
    } catch { /* ignore */ }
  };

  const handleRollback = async (version: number) => {
    if (!id || isNew || !confirm(`Rollback to version ${version}?`)) return;
    try { await rollbackAgent(Number(id), version); window.location.reload(); } catch { /* ignore */ }
  };

  const handleExpandExecution = async (execId: number) => {
    if (expandedExec === execId) { setExpandedExec(null); setExpandedExecDetail(null); return; }
    setExpandedExec(execId);
    try { const detail = await getAgentExecutionDetail(execId); setExpandedExecDetail(detail); } catch { /* ignore */ }
  };

  const toolsForWorkflow = form.tools.map(t => {
    const toolInfo = availableToolsList.find((at: any) => at.id === t.tool_id);
    return { tool_id: t.tool_id, tool_key: toolInfo?.tool_key || 'unknown', display_name: toolInfo?.display_name || 'Unknown Tool' };
  });

  if (loading) return <><Header title="Agent Editor" /><LoadingSpinner /></>;

  return (
    <>
      <Header title={isNew ? 'Create Agent' : `Edit: ${form.display_name}`} />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">

        {/* Breadcrumb + Actions */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Link to="/admin/agent-builder" className="hover:text-accent">Agent Builder</Link>
            <ChevronRight size={14} />
            <span className="text-foreground font-medium">{isNew ? 'Create New' : form.display_name}</span>
            {isSystem && <span className="text-[10px] px-1.5 py-0.5 bg-foreground/80 text-background rounded font-medium ml-2">SYSTEM</span>}
            {/* Phase 3d — Customized badge */}
            {!isNew && isUserCustomized && (
              <span
                title="This agent's prompt or tools differ from the system default. Click 'Reset to Default' to restore."
                className="text-[10px] px-1.5 py-0.5 bg-amber-100 dark:bg-amber-500/20 text-amber-800 dark:text-amber-400 rounded font-medium ml-1 inline-flex items-center gap-1"
              >
                <Sparkles size={10} /> Customized
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled} onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))} className="rounded border-border" />
              <span className="text-muted-foreground">Enabled</span>
            </label>
            {/* Phase 3d — Reset to Default (only for registered system agents that have been customized) */}
            {!isNew && canonical?.registered && isUserCustomized && (
              <button
                onClick={handleResetToDefault}
                disabled={resetting || saving}
                title="Restore prompt + tools to the canonical (code) version. Current values are saved as a new version for rollback."
                className="px-4 py-2 text-sm font-medium border border-amber-300 text-amber-800 dark:text-amber-400 rounded-lg hover:bg-amber-50 dark:bg-amber-500/15 flex items-center gap-2 disabled:opacity-50"
              >
                {resetting ? <Loader2 size={14} className="animate-spin" /> : <Undo2 size={14} />}
                Reset to Default
              </button>
            )}
            {!isNew && (
              <button onClick={handlePublish} className="px-4 py-2 text-sm font-medium border border-border rounded-lg hover:bg-muted/40 flex items-center gap-2">
                <Layers size={14} /> Publish
              </button>
            )}
            <button
              onClick={handleSave}
              disabled={saving || (validation !== null && !validation.valid)}
              title={validation !== null && !validation.valid
                ? 'Cannot save: prompt is missing required placeholders. Use Reset to Default if you want to undo.'
                : undefined}
              className={`px-5 py-2 rounded-lg text-sm font-medium flex items-center gap-2 disabled:opacity-50 ${
                validation !== null && !validation.valid
                  ? 'bg-red-100 dark:bg-red-500/20 text-red-800 dark:text-red-400 border border-red-300 cursor-not-allowed'
                  : 'bg-accent text-accent-foreground hover:bg-accent/90'
              }`}
            >
              <Save size={14} /> {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>

        {/* Tab Bar */}
        <div className="bg-card rounded-xl shadow-sm border border-border">
          <div className="flex border-b border-border">
            {TABS.map(tab => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`px-5 py-3 text-sm font-medium flex items-center gap-2 border-b-2 transition-colors ${
                  activeTab === tab.key
                    ? 'border-accent text-accent'
                    : 'border-transparent text-muted-foreground hover:text-muted-foreground'
                }`}
              >
                <tab.icon size={15} /> {tab.label}
              </button>
            ))}
          </div>

          <div className="p-6">
            {/* ════════════ TAB 1: Configuration ════════════ */}
            {activeTab === 'config' && (
              <div className="space-y-6">
                {/* Agent Identity */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Display Name</label>
                    <input value={form.display_name} onChange={e => handleNameChange(e.target.value)}
                      className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:ring-2 focus:ring-ring focus:border-transparent"
                      placeholder="My Agent" />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Agent Key</label>
                    <input value={form.agent_key} onChange={e => setForm(f => ({ ...f, agent_key: e.target.value }))}
                      readOnly={!isNew}
                      className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-muted/40 font-mono focus:ring-2 focus:ring-ring"
                      placeholder="my_agent" />
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1">Description</label>
                  <textarea value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
                    rows={2} className="w-full px-3 py-2 text-sm border border-border rounded-lg focus:ring-2 focus:ring-ring"
                    placeholder="What does this agent do?" />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Category</label>
                    <select value={form.category} onChange={e => setForm(f => ({ ...f, category: e.target.value }))}
                      className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-card">
                      {CATEGORY_OPTIONS.map(c => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Tags</label>
                    <div className="flex flex-wrap gap-1.5 items-center p-2 border border-border rounded-lg min-h-[38px]">
                      {form.tags.map(tag => (
                        <span key={tag} className="flex items-center gap-1 text-xs bg-accent/10 text-accent px-2 py-0.5 rounded-full">
                          {tag}
                          <button onClick={() => removeTag(tag)} className="hover:text-red-500"><X size={10} /></button>
                        </span>
                      ))}
                      <input value={tagInput} onChange={e => setTagInput(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } }}
                        placeholder="Add tag..." className="text-sm border-none outline-none flex-1 min-w-[80px] bg-transparent" />
                    </div>
                  </div>
                </div>

                {/* Execution Mode */}
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-3">Execution Mode</label>
                  {isMasterAgent && (
                    <p className="text-xs text-muted-foreground mb-3">
                      The Master Agent always runs as a ReAct loop
                      (<span className="font-mono">run_decision_maker</span>), so this
                      setting is not read at runtime. It is shown for reference —
                      changing it has no effect.
                    </p>
                  )}
                  <div className={`grid grid-cols-3 gap-4 ${isMasterAgent ? 'opacity-60 pointer-events-none' : ''}`}>
                    {EXECUTION_MODES.map(mode => {
                      const isSelected = form.agent_type === mode.type || (mode.type === 'react' && form.agent_type === 'tool_use');
                      return (
                        <button key={mode.type} onClick={() => setForm(f => ({ ...f, agent_type: mode.type }))}
                          className={`text-left p-4 rounded-xl border-2 transition-all ${
                            isSelected
                              ? `border-${mode.color}-500 bg-${mode.color}-50 ring-2 ring-${mode.color}-200`
                              : 'border-border hover:border-border bg-card'
                          }`}
                        >
                          <div className="flex items-center gap-2 mb-2">
                            <mode.icon size={20} className={isSelected ? `text-${mode.color}-600` : 'text-muted-foreground'} />
                            <span className={`font-semibold text-sm ${isSelected ? `text-${mode.color}-700` : 'text-foreground'}`}>{mode.label}</span>
                          </div>
                          <p className="text-xs text-muted-foreground leading-relaxed">{mode.desc}</p>
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* System Prompt */}
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-1">System Prompt</label>
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    <span className="text-[10px] text-muted-foreground mr-1 leading-6">Insert:</span>
                    {PROMPT_VARIABLES.map(v => (
                      <button key={v} onClick={() => insertVariable(v)}
                        className="text-[11px] px-2 py-0.5 bg-muted text-muted-foreground rounded hover:bg-accent/15 hover:text-accent/80 font-mono transition-colors">
                        {v}
                      </button>
                    ))}
                  </div>
                  <textarea value={form.system_prompt}
                    onChange={e => handlePromptChange(e.target.value)}
                    disabled={canonical?.registered && canonical?.supports_user_prompt === false}
                    rows={16}
                    className={`w-full px-3 py-2 text-sm border rounded-lg font-mono min-h-[300px] focus:ring-2 ${
                      validation && !validation.valid
                        ? 'border-red-400 focus:ring-red-500 bg-red-50/30 dark:bg-red-500/10'
                        : 'border-border focus:ring-ring'
                    } ${canonical?.supports_user_prompt === false ? 'bg-muted/40 cursor-not-allowed' : ''}`}
                    placeholder="You are a helpful assistant..." />

                  {/* Phase 3d — system-managed banner for protected agents */}
                  {canonical?.registered && canonical?.supports_user_prompt === false && (
                    <div className="mt-2 flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-foreground">
                      <Info size={14} className="mt-0.5 flex-shrink-0" />
                      <div>
                        <strong>System-managed.</strong> This agent's prompt cannot be edited via the UI to prevent breaking inter-agent orchestration. Contact engineering to change it.
                      </div>
                    </div>
                  )}

                  {/* Phase 3d — placeholder validator for user-editable system agents */}
                  {canonical?.registered && canonical?.supports_user_prompt && (
                    canonical.required_placeholders && canonical.required_placeholders.length > 0
                      ? (
                        <div className={`mt-2 rounded-md border px-3 py-2 text-xs ${
                          validation && !validation.valid
                            ? 'border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/15 text-red-900'
                            : 'border-emerald-200 dark:border-emerald-500/20 bg-emerald-50 dark:bg-emerald-500/15 text-emerald-900'
                        }`}>
                          <div className="mb-1 font-semibold flex items-center gap-1.5">
                            {validation && !validation.valid
                              ? <><AlertTriangle size={14} /> Required placeholders missing</>
                              : <><CheckCircle2 size={14} /> Required placeholders present</>}
                          </div>
                          <ul className="space-y-0.5 pl-1">
                            {canonical.required_placeholders.map(p => {
                              const present = form.system_prompt.includes(p);
                              return (
                                <li key={p} className="font-mono text-[11px] flex items-center gap-1.5">
                                  {present
                                    ? <span className="text-emerald-700 dark:text-emerald-400">✓</span>
                                    : <span className="text-red-700 dark:text-red-400">✗</span>}
                                  <span>{p}</span>
                                  {!present && <span className="text-red-700 dark:text-red-400 italic ml-1">— missing</span>}
                                </li>
                              );
                            })}
                          </ul>
                          {validation?.warnings && validation.warnings.length > 0 && (
                            <div className="mt-2 pt-2 border-t border-current/20">
                              <div className="font-semibold text-[11px] mb-0.5">Soft warnings</div>
                              {validation.warnings.map((w, i) => (
                                <div key={i} className="text-[11px] italic">⚠ {w}</div>
                              ))}
                            </div>
                          )}
                        </div>
                      ) : null
                  )}
                </div>

                {/* Model Configuration */}
                <div>
                  <label className="block text-xs font-medium text-muted-foreground mb-3">Model Configuration</label>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    <div>
                      <label className="block text-[11px] text-muted-foreground mb-1">Model</label>
                      <select value={form.model} onChange={e => setForm(f => ({ ...f, model: e.target.value }))}
                        className="w-full px-2 py-2 text-sm border border-border rounded-lg bg-card">
                        {providerModels.length === 0 && form.model && (
                          <option value={form.model}>{form.model}</option>
                        )}
                        {providerModels.map(m => (
                          <option key={m.id} value={m.id}>
                            {m.id}
                            {m.tier ? ` — ${m.tier}` : ''}
                            {m.is_current ? '' : ' (superseded)'}
                          </option>
                        ))}
                        {/* An agent pinned to a model the catalog no longer
                            lists must still display its real value rather than
                            silently rendering as the first option. */}
                        {form.model && !catalog.models.some(m => m.id === form.model) && (
                          <option value={form.model}>{form.model} (unknown)</option>
                        )}
                      </select>
                    </div>
                    <div>
                      <label className="block text-[11px] text-muted-foreground mb-1">Provider</label>
                      <select value={form.provider} onChange={e => {
                        const newProvider = e.target.value;
                        const valid = catalog.models.filter(m => m.provider === newProvider);
                        setForm(f => ({
                          ...f,
                          provider: newProvider,
                          // If the current model isn't valid for the new provider, switch to that
                          // provider's current worker-tier model. Without this, the dropdown
                          // displays the first option but form.model retains the old value, so
                          // saved settings and hint text point to the wrong model.
                          model: valid.some(m => m.id === f.model)
                            ? f.model
                            : (valid.find(m => m.is_current && m.tier === 'worker')
                               || valid.find(m => m.is_current)
                               || valid[0])?.id || '',
                        }));
                      }}
                        className="w-full px-2 py-2 text-sm border border-border rounded-lg bg-card">
                        {catalog.providers.map(p => <option key={p} value={p}>{p}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="block text-[11px] text-muted-foreground mb-1">
                        Temperature ({form.temperature})
                        {selectedModel && !selectedModel.supports_sampling && (
                          <span className="ml-1 text-muted-foreground/70">— not used by this model</span>
                        )}
                      </label>
                      {/* Sampling params were removed on the current Claude
                          family; the backend omits temperature for those models
                          because sending it is a 400. Showing a live slider
                          that changes nothing is worse than showing a disabled
                          one that says so. */}
                      <input type="range" min={0} max={1} step={0.1} value={form.temperature}
                        disabled={!!selectedModel && !selectedModel.supports_sampling}
                        onChange={e => setForm(f => ({ ...f, temperature: parseFloat(e.target.value) }))}
                        className="w-full mt-2 accent-blue-600 disabled:opacity-40 disabled:cursor-not-allowed" />
                    </div>
                    <div>
                      <label className="block text-[11px] text-muted-foreground mb-1">Max Tokens</label>
                      <input
                        type="number"
                        value={form.max_tokens}
                        min={MIN_MAX_TOKENS}
                        max={modelMaxTokens}
                        step={1024}
                        onChange={e => setForm(f => ({ ...f, max_tokens: parseInt(e.target.value) || 4096 }))}
                        onBlur={e => {
                          const raw = parseInt(e.target.value) || 4096;
                          const modelLimit = modelMaxTokens;
                          const clamped = Math.min(Math.max(raw, MIN_MAX_TOKENS), modelLimit);
                          if (clamped !== raw) {
                            setForm(f => ({ ...f, max_tokens: clamped }));
                          }
                        }}
                        className="w-full px-2 py-2 text-sm border border-border rounded-lg"
                      />
                      <p className="text-[10px] text-muted-foreground mt-1">
                        Max for {form.model}: {modelMaxTokens.toLocaleString()} completion tokens.
                        {form.max_tokens > modelMaxTokens && (
                          <span className="text-red-500 ml-1">Exceeds model ceiling — will be clamped on save.</span>
                        )}
                      </p>
                    </div>
                  </div>
                </div>

                {/* Extended Thinking & Effort */}
                {form.provider === 'anthropic' && (
                  <div className="bg-purple-50 dark:bg-purple-500/15 border border-purple-200 dark:border-purple-500/20 rounded-xl p-4">
                    <h4 className="text-xs font-semibold text-purple-700 dark:text-purple-400 mb-3 flex items-center gap-2"><Zap size={14} /> Extended Thinking & Effort</h4>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-[11px] text-purple-600 dark:text-purple-400 mb-1">Thinking Mode</label>
                        <select value={form.thinking_mode}
                          onChange={e => setForm(f => ({ ...f, thinking_mode: e.target.value }))}
                          className="w-full px-2 py-2 text-sm border border-purple-200 dark:border-purple-500/20 rounded-lg bg-card">
                          <option value="">Platform Default</option>
                          <option value="auto">Auto (adaptive where supported, off elsewhere)</option>
                          {/* Options are gated on what the selected model
                              actually accepts. Offering "Enabled" on a model
                              that removed budget_tokens is not a cosmetic
                              problem — the request fails with a 400. */}
                          {(!selectedModel || selectedModel.supports_adaptive_thinking) && (
                            <option value="adaptive">Adaptive (Claude decides thinking depth)</option>
                          )}
                          {(!selectedModel || selectedModel.supports_thinking_budget) && (
                            <option value="enabled">Enabled (manual budget control)</option>
                          )}
                          <option value="disabled">Disabled</option>
                        </select>
                      </div>
                      <div>
                        <label className="block text-[11px] text-purple-600 dark:text-purple-400 mb-1">Effort Level</label>
                        <select value={form.effort}
                          onChange={e => setForm(f => ({ ...f, effort: e.target.value }))}
                          className="w-full px-2 py-2 text-sm border border-purple-200 dark:border-purple-500/20 rounded-lg bg-card">
                          <option value="">Platform Default (high)</option>
                          {catalog.effort_levels
                            .filter(level =>
                              (level !== 'max' && level !== 'xhigh')
                              || !selectedModel
                              || selectedModel.supports_max_effort)
                            .map(level => (
                              <option key={level} value={level}>
                                {EFFORT_LABELS[level] || level}
                              </option>
                            ))}
                        </select>
                      </div>
                      {form.thinking_mode === 'enabled' && (
                        <div>
                          <label className="block text-[11px] text-purple-600 dark:text-purple-400 mb-1">Thinking Budget Tokens</label>
                          <input type="number" min={1000} max={128000} step={1000}
                            value={form.thinking_budget_tokens}
                            onChange={e => setForm(f => ({ ...f, thinking_budget_tokens: parseInt(e.target.value) || 10000 }))}
                            className="w-full px-2 py-2 text-sm border border-purple-200 dark:border-purple-500/20 rounded-lg bg-card" />
                          <p className="text-[10px] text-purple-400 mt-1">Must be less than max_tokens. Recommended: 10,000-32,000</p>
                        </div>
                      )}
                    </div>
                    <p className="text-[10px] text-purple-400 mt-2">
                      {selectedModel
                        ? (selectedModel.supports_adaptive_thinking
                            ? `${selectedModel.id} uses adaptive thinking — Claude decides the depth. Effort controls overall token spend.`
                            : `${selectedModel.id} needs an explicit thinking budget; adaptive is not available on it.`)
                        : 'Effort controls how deeply Claude reasons about each request.'}
                      {selectedModel && !selectedModel.supports_sampling && (
                        <> This model ignores Temperature — sampling controls were removed on it.</>
                      )}
                    </p>
                  </div>
                )}

                {/* LangChain Config */}
                {(form.agent_type === 'react' || form.agent_type === 'tool_use') && (
                  <div className="bg-accent/10 border border-accent/20 rounded-xl p-4">
                    <h4 className="text-xs font-semibold text-accent mb-3 flex items-center gap-2"><Zap size={14} /> LangChain Configuration</h4>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-[11px] text-accent mb-1">Max Iterations</label>
                        <input type="number" min={1} max={50}
                          value={form.langchain_config?.max_iterations || 10}
                          onChange={e => setForm(f => ({ ...f, langchain_config: { ...f.langchain_config, max_iterations: parseInt(e.target.value) || 10 } }))}
                          className="w-full px-2 py-2 text-sm border border-accent/20 rounded-lg bg-card" />
                      </div>
                      <div className="flex items-center pt-4">
                        <label className="flex items-center gap-2 text-sm text-accent cursor-pointer">
                          <input type="checkbox" checked={form.langchain_config?.return_intermediate_steps ?? true}
                            onChange={e => setForm(f => ({ ...f, langchain_config: { ...f.langchain_config, return_intermediate_steps: e.target.checked } }))}
                            className="rounded border-accent/40 text-accent" />
                          Return intermediate steps
                        </label>
                      </div>
                    </div>
                  </div>
                )}

                {/* Schemas */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Input Schema (JSON)</label>
                    <textarea value={form.input_schema} onChange={e => setForm(f => ({ ...f, input_schema: e.target.value }))}
                      rows={4} className="w-full px-3 py-2 text-xs border border-border rounded-lg font-mono" />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-muted-foreground mb-1">Output Schema (JSON)</label>
                    <textarea value={form.output_schema} onChange={e => setForm(f => ({ ...f, output_schema: e.target.value }))}
                      rows={4} className="w-full px-3 py-2 text-xs border border-border rounded-lg font-mono" />
                  </div>
                </div>

                {/* Auto-Learning */}
                <div className="bg-purple-50 dark:bg-purple-500/15 border border-purple-100 rounded-xl p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <h4 className="text-sm font-semibold text-purple-800 dark:text-purple-400">Auto-Learning</h4>
                      <p className="text-xs text-purple-600 dark:text-purple-400 mt-0.5">
                        Automatically extract and store learnings from interactions into long-term memory
                      </p>
                    </div>
                    <label className="relative inline-flex items-center cursor-pointer">
                      <input type="checkbox" checked={form.learning_enabled}
                        onChange={e => setForm(f => ({ ...f, learning_enabled: e.target.checked }))}
                        className="sr-only peer" />
                      <div className="w-9 h-5 bg-muted peer-focus:ring-2 peer-focus:ring-purple-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-card after:border-border after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-purple-600"></div>
                    </label>
                  </div>
                </div>
              </div>
            )}

            {/* ════════════ TAB 2: Tools ════════════ */}
            {activeTab === 'tools' && (
              <div className="space-y-6">
                {/* What this agent ACTUALLY runs with. The panel below edits
                    the stored `tools` column, which for several agents is not
                    what reaches the model: the Master Agent builds its catalog
                    per run and stores nothing, and two agents run fixed sets
                    from their handler. Showing only the stored column made the
                    Master Agent read "Assigned Tools (0)" while running with
                    more than forty. */}
                {effectiveTools && (
                  <div className={`rounded-xl border p-5 ${
                    effectiveTools.source === 'configured'
                      ? 'border-border bg-card'
                      : 'border-blue-500/30 bg-blue-500/5'
                  }`}>
                    <div className="flex items-center justify-between mb-1">
                      <h3 className="text-sm font-semibold text-foreground">
                        Tools this agent actually runs with ({effectiveTools.tools.length})
                      </h3>
                      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        source: {effectiveTools.source}
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground mb-3">{effectiveTools.note}</p>
                    {effectiveTools.tools.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {effectiveTools.tools.map((t: any) => (
                          <span
                            key={t.name}
                            title={
                              t.tier === 'read'
                                ? 'Runs without asking'
                                : t.tier === 'destructive'
                                  ? 'Irreversible — always asks first'
                                  : 'Changes data — asks first'
                            }
                            className={`text-[11px] font-mono px-2 py-0.5 rounded border ${
                              t.tier === 'destructive'
                                ? 'border-destructive/40 text-destructive'
                                : t.tier === 'write'
                                  ? 'border-amber-500/40 text-amber-700 dark:text-amber-400'
                                  : 'border-border text-muted-foreground'
                            }`}
                          >
                            {t.name}
                          </span>
                        ))}
                      </div>
                    )}
                    {effectiveTools.source !== 'configured' && (
                      <p className="text-[11px] text-muted-foreground mt-3">
                        Editing the assignment below will not change this list.
                      </p>
                    )}
                  </div>
                )}

                <ToolConfigPanel
                  assignedTools={form.tools}
                  onChange={tools => setForm(f => ({ ...f, tools }))}
                />

                {/* MCP Server Assignment */}
                {(form.agent_type === 'react' || form.agent_type === 'tool_use') && (
                  <div className="bg-card border border-border rounded-xl p-5">
                    <h3 className="text-sm font-semibold text-foreground mb-1">MCP Servers</h3>
                    <p className="text-xs text-muted-foreground mb-4">
                      Select which external MCP servers this agent can access. Tools from selected servers will be available during execution.
                    </p>
                    {mcpServers.length === 0 ? (
                      <p className="text-xs text-muted-foreground italic">No MCP servers configured. Add servers in the MCP Servers admin page.</p>
                    ) : (
                      <div className="space-y-2">
                        {mcpServers.map((server: any) => (
                          <label key={server.server_name} className="flex items-center gap-3 p-2.5 rounded-lg border border-border hover:bg-muted/40 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={form.mcp_servers.includes(server.server_name)}
                              onChange={e => {
                                if (e.target.checked) {
                                  setForm(f => ({ ...f, mcp_servers: [...f.mcp_servers, server.server_name] }));
                                } else {
                                  setForm(f => ({ ...f, mcp_servers: f.mcp_servers.filter((s: string) => s !== server.server_name) }));
                                }
                              }}
                              className="rounded border-border text-accent"
                            />
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="text-sm font-medium text-foreground">{server.display_name || server.server_name}</span>
                                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${server.is_enabled ? 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400' : 'bg-muted text-muted-foreground'}`}>
                                  {server.is_enabled ? 'Active' : 'Disabled'}
                                </span>
                                <span className="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded font-mono">
                                  {server.transport_type}
                                </span>
                              </div>
                              {server.description && (
                                <p className="text-xs text-muted-foreground mt-0.5 truncate">{server.description}</p>
                              )}
                            </div>
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* ════════════ TAB 3: Workflow ════════════ */}
            {activeTab === 'workflow' && (
              <WorkflowEditor
                agentType={form.agent_type}
                tools={toolsForWorkflow}
                orchestrationConfig={form.orchestration_config}
                onOrchestrationConfigChange={config => setForm(f => ({ ...f, orchestration_config: config }))}
                availableAgents={availableAgents.map((a: any) => ({ agent_key: a.agent_key, display_name: a.display_name }))}
              />
            )}

            {/* ════════════ TAB 4: Training Data ════════════ */}
            {activeTab === 'training' && (
              <div className="space-y-6">
                <div>
                  <h3 className="text-sm font-semibold text-foreground mb-1">Assigned Training Datasets</h3>
                  <p className="text-xs text-muted-foreground mb-4">
                    Training datasets provide knowledge context that is injected into this agent's system prompt at execution time.
                    Manage datasets in the <a href="/admin/training-datasets" className="text-indigo-600 dark:text-indigo-400 hover:underline">Training Datasets</a> page.
                  </p>

                  {loadingDatasets ? (
                    <div className="text-center py-8 text-muted-foreground">
                      <Loader2 className="animate-spin mx-auto mb-2" size={20} />
                      Loading datasets...
                    </div>
                  ) : (
                    <>
                      {/* Currently assigned */}
                      {assignedDatasets.length > 0 && (
                        <div className="mb-6">
                          <h4 className="text-xs font-semibold text-muted-foreground uppercase mb-2">Currently Assigned</h4>
                          <div className="space-y-2">
                            {assignedDatasets.map((ad: any) => (
                              <div key={ad.dataset_id} className="flex items-center gap-3 px-4 py-3 bg-indigo-50 dark:bg-indigo-500/15 border border-indigo-200 dark:border-indigo-500/20 rounded-lg">
                                <Layers size={16} className="text-indigo-500 shrink-0" />
                                <div className="flex-1 min-w-0">
                                  <span className="text-sm font-medium text-foreground">{ad.name}</span>
                                  {ad.description && (
                                    <p className="text-xs text-muted-foreground truncate">{ad.description}</p>
                                  )}
                                  <div className="flex items-center gap-2 mt-1">
                                    {(ad.tags || []).map((t: string, i: number) => (
                                      <span key={i} className="px-1.5 py-0.5 bg-indigo-100 dark:bg-indigo-500/20 text-indigo-600 dark:text-indigo-400 rounded text-xs">{t}</span>
                                    ))}
                                    <span className="text-xs text-muted-foreground">{ad.file_count} file{ad.file_count !== 1 ? 's' : ''}</span>
                                    <span className="text-xs text-muted-foreground">Priority: {ad.priority}</span>
                                  </div>
                                </div>
                                <button
                                  onClick={async () => {
                                    try {
                                      await unassignDatasetFromAgent(ad.dataset_id, Number(id));
                                      setAssignedDatasets(prev => prev.filter(d => d.dataset_id !== ad.dataset_id));
                                    } catch (e) { console.error(e); }
                                  }}
                                  className="p-1.5 text-muted-foreground hover:text-red-600 dark:text-red-400 transition"
                                  title="Remove"
                                >
                                  <X size={16} />
                                </button>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* Available to assign */}
                      <div>
                        <h4 className="text-xs font-semibold text-muted-foreground uppercase mb-2">Available Datasets</h4>
                        {allDatasets.filter(d => !assignedDatasets.some((ad: any) => ad.dataset_id === d.id)).length === 0 ? (
                          <p className="text-sm text-muted-foreground py-4 text-center bg-muted/40 rounded-lg">
                            {allDatasets.length === 0
                              ? 'No training datasets created yet. Create one in the Training Datasets page.'
                              : 'All available datasets are already assigned.'}
                          </p>
                        ) : (
                          <div className="space-y-2">
                            {allDatasets
                              .filter(d => !assignedDatasets.some((ad: any) => ad.dataset_id === d.id))
                              .map(d => (
                                <div key={d.id} className="flex items-center gap-3 px-4 py-3 bg-card border border-border rounded-lg">
                                  <Layers size={16} className="text-muted-foreground shrink-0" />
                                  <div className="flex-1 min-w-0">
                                    <span className="text-sm font-medium text-foreground">{d.name}</span>
                                    {d.description && (
                                      <p className="text-xs text-muted-foreground truncate">{d.description}</p>
                                    )}
                                    <div className="flex items-center gap-2 mt-1">
                                      {(d.tags || []).map((t: string, i: number) => (
                                        <span key={i} className="px-1.5 py-0.5 bg-muted text-muted-foreground rounded text-xs">{t}</span>
                                      ))}
                                      <span className="text-xs text-muted-foreground">{d.file_count} file{d.file_count !== 1 ? 's' : ''}</span>
                                    </div>
                                  </div>
                                  <button
                                    onClick={async () => {
                                      setAssigningDataset(d.id);
                                      try {
                                        await assignDatasetToAgent(d.id, Number(id), assignedDatasets.length);
                                        const updated = await getAgentTrainingDatasets(Number(id));
                                        setAssignedDatasets(updated);
                                      } catch (e) { console.error(e); }
                                      finally { setAssigningDataset(null); }
                                    }}
                                    disabled={assigningDataset === d.id}
                                    className="px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1"
                                  >
                                    {assigningDataset === d.id ? (
                                      <Loader2 size={12} className="animate-spin" />
                                    ) : (
                                      <Plus size={12} />
                                    )}
                                    Assign
                                  </button>
                                </div>
                              ))}
                          </div>
                        )}
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* ════════════ TAB 5: Test & Debug ════════════ */}
            {activeTab === 'test' && (
              <div className="space-y-6">
                {/* Quick Test */}
                <div>
                  <h3 className="text-sm font-semibold text-foreground mb-3">Quick Test</h3>
                  <textarea value={testInput} onChange={e => setTestInput(e.target.value)}
                    rows={5} className="w-full px-3 py-2 text-sm border border-border rounded-lg font-mono focus:ring-2 focus:ring-ring"
                    placeholder='{"query": "Analyze tender #123"}\nor just type a message' />

                  {/* Document Upload Section */}
                  <div className="mt-3 p-3 border border-dashed border-border rounded-lg bg-muted/40/50">
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="text-xs font-semibold text-muted-foreground flex items-center gap-1.5">
                        <Upload size={13} />
                        Test Documents
                        <span className="font-normal text-muted-foreground">(PDF, DOCX, images)</span>
                      </h4>
                      {storedDocs.length > 0 && (
                        <button onClick={handleDeleteAllDocs} className="text-xs text-muted-foreground hover:text-red-500 transition-colors">
                          Clear all
                        </button>
                      )}
                    </div>

                    {/* Stored Documents (from DB) */}
                    {storedDocs.length > 0 && (
                      <div className="space-y-1.5 mb-2">
                        {storedDocs.map((doc) => (
                          <div key={doc.id} className="flex items-center gap-2 bg-card border border-border rounded-md px-2.5 py-1.5">
                            <FileText size={13} className={doc.extraction_status === 'success' ? 'text-green-500 shrink-0' : doc.extraction_status === 'failed' ? 'text-red-400 shrink-0' : 'text-accent shrink-0'} />
                            <span className="text-xs text-foreground truncate flex-1">{doc.file_name}</span>
                            {doc.page_count && <span className="text-[10px] text-muted-foreground shrink-0">{doc.page_count}p</span>}
                            <span className="text-[10px] text-muted-foreground shrink-0">{(doc.file_size / 1024).toFixed(0)} KB</span>
                            {doc.extraction_status === 'success' && <CheckCircle2 size={12} className="text-green-500 shrink-0" />}
                            {doc.extraction_status === 'failed' && (
                              <button onClick={() => handleReExtract(doc.id)} title="Re-extract text" className="text-amber-500 hover:text-amber-600 dark:text-amber-400 shrink-0">
                                <RefreshCw size={12} />
                              </button>
                            )}
                            {doc.extraction_status === 'partial' && (
                              <span title="Partial extraction - some pages may be empty" className="text-amber-400 shrink-0">
                                <Info size={12} />
                              </span>
                            )}
                            <button onClick={() => handleDeleteDoc(doc.id)}
                              className="text-muted-foreground/50 hover:text-red-500 transition-colors shrink-0">
                              <Trash2 size={12} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Pending uploads (not yet saved) */}
                    {testFiles.length > 0 && (
                      <div className="space-y-1.5 mb-2">
                        {testFiles.map((file, idx) => (
                          <div key={`new-${idx}`} className="flex items-center gap-2 bg-amber-50 dark:bg-amber-500/15 border border-amber-200 dark:border-amber-500/20 rounded-md px-2.5 py-1.5">
                            <FileText size={13} className="text-amber-500 shrink-0" />
                            <span className="text-xs text-foreground truncate flex-1">{file.name}</span>
                            <span className="text-[10px] text-amber-500 shrink-0">pending</span>
                            <span className="text-[10px] text-muted-foreground shrink-0">{(file.size / 1024).toFixed(0)} KB</span>
                            <button onClick={() => setTestFiles(prev => prev.filter((_, i) => i !== idx))}
                              className="text-muted-foreground/50 hover:text-red-500 transition-colors shrink-0">
                              <Trash2 size={12} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}

                    <label className={`flex items-center justify-center gap-2 px-3 py-2 border border-border rounded-md bg-card text-sm text-muted-foreground hover:bg-muted/40 cursor-pointer transition-colors ${uploadingDocs ? 'opacity-50 pointer-events-none' : ''}`}>
                      {uploadingDocs ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}
                      {uploadingDocs ? 'Uploading...' : storedDocs.length === 0 && testFiles.length === 0 ? 'Upload documents to test context understanding' : 'Add more files'}
                      <input
                        type="file"
                        multiple
                        accept=".pdf,.docx,.doc,.png,.jpg,.jpeg,.tiff,.bmp"
                        className="hidden"
                        disabled={uploadingDocs}
                        onChange={(e) => {
                          if (e.target.files && e.target.files.length > 0) {
                            handleUploadDocuments(e.target.files);
                          }
                          e.target.value = '';
                        }}
                      />
                    </label>
                    {(storedDocs.length > 0 || testFiles.length > 0) && (
                      <p className="text-[10px] text-muted-foreground mt-1.5">
                        {storedDocs.length} stored document{storedDocs.length !== 1 ? 's' : ''} will be included in test context.
                        {testFiles.length > 0 && ` ${testFiles.length} pending file${testFiles.length !== 1 ? 's' : ''} will be uploaded on Run Test.`}
                      </p>
                    )}
                  </div>

                  <div className="flex gap-3 mt-3">
                    <button onClick={handleTest} disabled={testRunning || isNew}
                      className="bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 flex items-center gap-2 disabled:opacity-50">
                      {testRunning ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
                      {testRunning ? 'Running...' : (storedDocs.length + testFiles.length) > 0 ? `Run Test (${storedDocs.length + testFiles.length} doc${(storedDocs.length + testFiles.length) > 1 ? 's' : ''})` : 'Run Test'}
                    </button>
                    {testCases.length > 0 && (
                      <button onClick={handleRunAllTests} disabled={runningAllTests || isNew}
                        className="px-4 py-2 border border-border rounded-lg text-sm font-medium hover:bg-muted/40 flex items-center gap-2 disabled:opacity-50">
                        {runningAllTests ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                        Run All Tests ({testCases.length})
                      </button>
                    )}
                  </div>
                </div>

                {/* Execution Trace */}
                {testOutput && (
                  <div>
                    <h3 className="text-sm font-semibold text-foreground mb-3">Execution Result</h3>
                    {testOutput.error ? (
                      <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-4 text-sm text-red-600 dark:text-red-400">{testOutput.error}</div>
                    ) : (
                      <>
                        {testOutput.tool_calls && testOutput.tool_calls.length > 0 && (
                          <ExecutionTraceViewer
                            steps={testOutput.tool_calls.map((tc: any) => ({
                              type: 'tool_call' as const,
                              tool_name: tc.tool,
                              input: tc.input,
                              output: tc.output,
                              latency_ms: tc.latency_ms,
                              status: tc.status || 'success',
                            }))}
                            finalOutput={typeof testOutput.output === 'string' ? testOutput.output : JSON.stringify(testOutput.output, null, 2)}
                            totalTokens={(testOutput.tokens_input || 0) + (testOutput.tokens_output || 0)}
                            totalLatencyMs={testOutput.latency_ms}
                            totalCost={testOutput.cost_estimate}
                          />
                        )}
                        {(!testOutput.tool_calls || testOutput.tool_calls.length === 0) && (
                          <div className="bg-muted/40 border border-border rounded-lg p-4">
                            <pre className="text-sm text-foreground whitespace-pre-wrap font-mono max-h-64 overflow-auto">
                              {typeof testOutput.output === 'string' ? testOutput.output : JSON.stringify(testOutput.output, null, 2)}
                            </pre>
                            <div className="flex gap-4 text-[11px] text-muted-foreground pt-3 mt-3 border-t border-border">
                              {testOutput.tokens_input != null && <span className="flex items-center gap-1"><Hash size={11} /> {testOutput.tokens_input + (testOutput.tokens_output || 0)} tokens</span>}
                              {testOutput.latency_ms != null && <span className="flex items-center gap-1"><Clock size={11} /> {testOutput.latency_ms}ms</span>}
                              {testOutput.cost_estimate != null && <span className="flex items-center gap-1"><Coins size={11} /> ${testOutput.cost_estimate.toFixed(4)}</span>}
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )}

                {/* Test Cases */}
                <div>
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-semibold text-foreground">Test Cases</h3>
                  </div>
                  {testCases.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4 text-center">No test cases yet. Use the Testing page to add test cases.</p>
                  ) : (
                    <div className="space-y-2">
                      {testCases.map((tc: any) => {
                        const result = testResults[tc.id];
                        return (
                          <div key={tc.id} className="bg-muted/40 border border-border rounded-lg p-3 flex items-center justify-between">
                            <div>
                              <span className="text-sm font-medium text-foreground">{tc.test_name}</span>
                              <p className="text-xs text-muted-foreground mt-0.5">
                                Input: {JSON.stringify(tc.input_data).substring(0, 80)}...
                              </p>
                            </div>
                            <div className="flex items-center gap-3">
                              {result && (
                                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${result.passed ? 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400' : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'}`}>
                                  {result.passed ? 'Pass' : 'Fail'} {result.score != null ? `(${(result.score * 100).toFixed(0)}%)` : ''}
                                </span>
                              )}
                              <button onClick={() => handleRunSingleTest(tc.id)}
                                className="text-xs text-accent hover:text-accent/80 flex items-center gap-1">
                                <Play size={12} /> Run
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* ════════════ TAB 5: Monitoring ════════════ */}
            {activeTab === 'monitoring' && (
              <div className="space-y-6">
                {/* Stats Cards */}
                {stats && (
                  <div className="grid grid-cols-4 gap-4">
                    <div className="bg-accent/10 rounded-lg p-4">
                      <p className="text-xs text-accent font-medium">Total Executions</p>
                      <p className="text-2xl font-bold text-accent mt-1">{stats.total_executions || 0}</p>
                    </div>
                    <div className="bg-emerald-50 dark:bg-emerald-500/15 rounded-lg p-4">
                      <p className="text-xs text-emerald-500 font-medium">Success Rate</p>
                      <p className="text-2xl font-bold text-emerald-700 dark:text-emerald-400 mt-1">{((stats.success_rate || 0) * 100).toFixed(1)}%</p>
                    </div>
                    <div className="bg-amber-50 dark:bg-amber-500/15 rounded-lg p-4">
                      <p className="text-xs text-amber-500 font-medium">Avg Latency</p>
                      <p className="text-2xl font-bold text-amber-700 dark:text-amber-400 mt-1">{Math.round(stats.avg_latency_ms || 0)}ms</p>
                    </div>
                    <div className="bg-purple-50 dark:bg-purple-500/15 rounded-lg p-4">
                      <p className="text-xs text-purple-500 font-medium">Total Cost</p>
                      <p className="text-2xl font-bold text-purple-700 dark:text-purple-400 mt-1">${(stats.total_cost || 0).toFixed(2)}</p>
                    </div>
                  </div>
                )}

                {/* Sub-tabs */}
                <div className="flex gap-4 border-b border-border">
                  <button onClick={() => setMonitorSubTab('executions')}
                    className={`pb-2 text-sm font-medium border-b-2 ${monitorSubTab === 'executions' ? 'border-accent text-accent' : 'border-transparent text-muted-foreground'}`}>
                    Execution History
                  </button>
                  <button onClick={() => setMonitorSubTab('versions')}
                    className={`pb-2 text-sm font-medium border-b-2 ${monitorSubTab === 'versions' ? 'border-accent text-accent' : 'border-transparent text-muted-foreground'}`}>
                    Version History
                  </button>
                </div>

                {monitorSubTab === 'executions' && (
                  <div>
                    {executions.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-8 text-center">No executions yet</p>
                    ) : (
                      <div className="space-y-2">
                        {executions.map((exec: any) => (
                          <div key={exec.id} className="border border-border rounded-lg">
                            <button onClick={() => handleExpandExecution(exec.id)}
                              className="w-full p-3 flex items-center gap-4 text-sm hover:bg-muted/40 transition-colors">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                                exec.status === 'completed' ? 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400'
                                : exec.status === 'failed' ? 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'
                                : exec.status === 'running' ? 'bg-accent/15 text-accent'
                                : 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400'
                              }`}>{exec.status}</span>
                              <span className="text-muted-foreground">v{exec.agent_version}</span>
                              <span className="text-muted-foreground text-xs">{exec.trigger}</span>
                              <span className="text-muted-foreground text-xs ml-auto flex items-center gap-1"><Clock size={11} /> {exec.latency_ms}ms</span>
                              <span className="text-muted-foreground text-xs flex items-center gap-1"><Coins size={11} /> ${(exec.cost_estimate || 0).toFixed(4)}</span>
                              <span className="text-muted-foreground text-xs">{formatDateTime(exec.created_at)}</span>
                              {expandedExec === exec.id ? <ChevronRight size={14} className="rotate-90" /> : <ChevronRight size={14} />}
                            </button>
                            {expandedExec === exec.id && expandedExecDetail && (
                              <div className="px-4 pb-4 space-y-3 border-t border-border">
                                <div>
                                  <p className="text-xs font-semibold text-muted-foreground mt-3 mb-1">Input</p>
                                  <pre className="text-xs bg-muted/40 border border-border rounded p-2 max-h-32 overflow-auto font-mono">
                                    {expandedExecDetail.input_summary}
                                  </pre>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-muted-foreground mb-1">Output</p>
                                  <pre className="text-xs bg-muted/40 border border-border rounded p-2 max-h-48 overflow-auto font-mono">
                                    {expandedExecDetail.output_summary}
                                  </pre>
                                </div>
                                {expandedExecDetail.error_message && (
                                  <div>
                                    <p className="text-xs font-semibold text-red-500 mb-1">Error</p>
                                    <p className="text-xs bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded p-2 text-red-600 dark:text-red-400">{expandedExecDetail.error_message}</p>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {monitorSubTab === 'versions' && (
                  <div>
                    {versions.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-8 text-center">No version history yet</p>
                    ) : (
                      <div className="space-y-2">
                        {versions.map((v: any, idx: number) => (
                          <div key={v.id} className="bg-muted/40 border border-border rounded-lg p-4">
                            <div className="flex items-center justify-between">
                              <div>
                                <span className="text-sm font-semibold text-foreground">v{v.version_number}</span>
                                <p className="text-xs text-muted-foreground mt-0.5">{v.change_description || 'No description'}</p>
                                <div className="flex items-center gap-3 mt-1 text-[11px] text-muted-foreground">
                                  <span>{v.model}</span>
                                  <span>temp: {v.temperature}</span>
                                  <span>tokens: {v.max_tokens}</span>
                                  <span>{formatDateTime(v.created_at)}</span>
                                </div>
                              </div>
                              <div className="flex items-center gap-2">
                                {idx > 0 && (
                                  <button onClick={() => setDiffVersions([versions[idx], versions[idx - 1]])}
                                    className="text-xs text-accent hover:text-accent/80 flex items-center gap-1">
                                    <Code size={12} /> Diff
                                  </button>
                                )}
                                <button onClick={() => handleRollback(v.version_number)}
                                  className="text-xs text-muted-foreground hover:text-accent flex items-center gap-1">
                                  <RotateCcw size={12} /> Rollback
                                </button>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Diff View */}
                    {diffVersions && (
                      <div className="mt-4 bg-card border border-accent/20 rounded-lg p-4">
                        <div className="flex items-center justify-between mb-3">
                          <h4 className="text-sm font-semibold text-accent">
                            Comparing v{diffVersions[0].version_number} → v{diffVersions[1].version_number}
                          </h4>
                          <button onClick={() => setDiffVersions(null)} className="text-muted-foreground hover:text-muted-foreground"><X size={16} /></button>
                        </div>
                        <div className="space-y-3 text-xs">
                          {diffVersions[0].model !== diffVersions[1].model && (
                            <div><span className="text-muted-foreground font-medium">Model:</span> <span className="text-red-500 line-through">{diffVersions[0].model}</span> → <span className="text-green-600 dark:text-green-400">{diffVersions[1].model}</span></div>
                          )}
                          {diffVersions[0].temperature !== diffVersions[1].temperature && (
                            <div><span className="text-muted-foreground font-medium">Temperature:</span> <span className="text-red-500">{diffVersions[0].temperature}</span> → <span className="text-green-600 dark:text-green-400">{diffVersions[1].temperature}</span></div>
                          )}
                          {diffVersions[0].max_tokens !== diffVersions[1].max_tokens && (
                            <div><span className="text-muted-foreground font-medium">Max Tokens:</span> <span className="text-red-500">{diffVersions[0].max_tokens}</span> → <span className="text-green-600 dark:text-green-400">{diffVersions[1].max_tokens}</span></div>
                          )}
                          {diffVersions[0].system_prompt !== diffVersions[1].system_prompt && (
                            <div>
                              <span className="text-muted-foreground font-medium">System Prompt: </span><span className="text-amber-600 dark:text-amber-400">changed</span>
                              <div className="grid grid-cols-2 gap-2 mt-1">
                                <pre className="bg-red-50 dark:bg-red-500/15 border border-red-100 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap text-red-700 dark:text-red-400">{(diffVersions[0].system_prompt || '').substring(0, 500)}</pre>
                                <pre className="bg-green-50 dark:bg-green-500/15 border border-green-100 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap text-green-700 dark:text-green-400">{(diffVersions[1].system_prompt || '').substring(0, 500)}</pre>
                              </div>
                            </div>
                          )}
                          {JSON.stringify(diffVersions[0].tools) !== JSON.stringify(diffVersions[1].tools) && (
                            <div><span className="text-muted-foreground font-medium">Tools:</span> <span className="text-amber-600 dark:text-amber-400">changed</span></div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
