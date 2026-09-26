import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { type Node, type Edge } from '@xyflow/react';
import {
  ArrowLeft, Save, Upload, CheckCircle2, AlertCircle, Clock,
  GitBranch, Code, Eye, Play, History, X,
} from 'lucide-react';
import {
  getWorkflowById, saveWorkflowGraphApi, publishWorkflow,
  validateWorkflow as validateWorkflowApi, getBuilderAgents,
  getWorkflowExecutions,
} from '../../lib/api';
import WorkflowCanvas, {
  toReactFlowNodes, toReactFlowEdges,
  fromReactFlowNodes, fromReactFlowEdges,
} from '../../components/workflows/WorkflowCanvas';
import WorkflowToolbox from '../../components/workflows/WorkflowToolbox';
import WorkflowPropertiesPanel from '../../components/workflows/WorkflowPropertiesPanel';
import WorkflowTestRunner from '../../components/workflows/WorkflowTestRunner';
import type { Workflow, WorkflowNode, NodeType, WorkflowValidation } from '../../types/workflow';

const DEFAULT_LABELS: Record<string, string> = {
  start: 'Start', end: 'End', agent: 'Agent', classify: 'Classify',
  if_else: 'If / Else', while_loop: 'While Loop', user_approval: 'Approval',
  transform: 'Transform', set_state: 'Set State', tool: 'Tool', note: 'Note',
};

let nodeCounter = 0;

export default function WorkflowEditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const workflowId = parseInt(id || '0');

  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [validation, setValidation] = useState<WorkflowValidation | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null);
  const [agents, setAgents] = useState<Array<{ agent_key: string; display_name: string; id: number }>>([]);
  const [editorMode, setEditorMode] = useState<'visual' | 'json'>('visual');
  const [showExecPanel, setShowExecPanel] = useState(false);
  const [bottomTab, setBottomTab] = useState<'test' | 'history'>('test');
  const [executions, setExecutions] = useState<any[]>([]);
  const [saveToast, setSaveToast] = useState('');
  const [activeTestNode, setActiveTestNode] = useState<string | null>(null);
  const [nodeConfigVersion, setNodeConfigVersion] = useState(0);

  // Canvas version key — only incremented on load, add, delete, or external updates
  const [canvasVersion, setCanvasVersion] = useState(0);

  // React Flow state refs
  const nodesRef = useRef<Node[]>([]);
  const edgesRef = useRef<Edge[]>([]);

  useEffect(() => {
    const load = async () => {
      try {
        const [wfData, agentData] = await Promise.all([
          getWorkflowById(workflowId),
          getBuilderAgents().catch(() => []),
        ]);
        setWorkflow(wfData);
        setAgents(agentData.map((a: any) => ({
          agent_key: a.agent_key,
          display_name: a.display_name,
          id: a.id,
        })));
        nodesRef.current = toReactFlowNodes(wfData.nodes);
        edgesRef.current = toReactFlowEdges(wfData.edges);
        setCanvasVersion((v) => v + 1);
      } catch (err) {
        console.error('Failed to load workflow:', err);
      } finally {
        setLoading(false);
      }
    };
    if (workflowId) load();
  }, [workflowId]);

  const handleSave = async () => {
    if (!workflow) return;
    setSaving(true);
    try {
      const result = await saveWorkflowGraphApi(workflow.id, {
        nodes: fromReactFlowNodes(nodesRef.current),
        edges: fromReactFlowEdges(edgesRef.current),
      });
      setWorkflow(result);
      setHasChanges(false);
      setValidation(null);
      setSaveToast('Saved');
      setTimeout(() => setSaveToast(''), 2000);
    } catch (err) {
      console.error('Failed to save:', err);
      setSaveToast('Save failed');
      setTimeout(() => setSaveToast(''), 3000);
    } finally {
      setSaving(false);
    }
  };

  const handlePublish = async () => {
    if (!workflow) return;
    // Save first if there are changes
    if (hasChanges) await handleSave();
    // Validate
    const val = await validateWorkflowApi(workflow.id);
    setValidation(val);
    if (!val.valid) return;

    setPublishing(true);
    try {
      const result = await publishWorkflow(workflow.id);
      setWorkflow(result);
      setHasChanges(false);
      setSaveToast('Published');
      setTimeout(() => setSaveToast(''), 2000);
    } catch (err) {
      console.error('Failed to publish:', err);
    } finally {
      setPublishing(false);
    }
  };

  const handleValidate = async () => {
    if (!workflow) return;
    if (hasChanges) await handleSave();
    const val = await validateWorkflowApi(workflow.id);
    setValidation(val);
  };

  const handleAddNode = useCallback((type: NodeType, position?: { x: number; y: number }) => {
    nodeCounter++;
    const key = `${type}_${Date.now()}_${nodeCounter}`;
    const pos = position || { x: 300 + Math.random() * 200, y: 200 + Math.random() * 100 };
    const defaultConfig: Record<string, any> = {};
    if (type === 'classify') defaultConfig.branches = [];
    if (type === 'while_loop') { defaultConfig.max_iterations = 10; defaultConfig.condition_expression = ''; }
    if (type === 'if_else') { defaultConfig.true_label = 'True'; defaultConfig.false_label = 'False'; defaultConfig.condition_expression = ''; }
    if (type === 'user_approval') { defaultConfig.prompt_template = 'Please approve this step.'; defaultConfig.timeout_seconds = 3600; }
    if (type === 'for_each') { defaultConfig.collection_expression = ''; defaultConfig.item_variable = 'current_item'; defaultConfig.index_variable = 'current_index'; defaultConfig.output_key = ''; defaultConfig.parallel = false; defaultConfig.concurrency_limit = 3; defaultConfig.continue_on_error = true; defaultConfig.max_iterations = 20; }
    if (type === 'parallel') { defaultConfig.branches = []; defaultConfig.merge_strategy = 'dict'; defaultConfig.output_key = ''; defaultConfig.timeout_seconds = 300; }

    const newNode: Node = {
      id: key,
      type,
      position: pos,
      data: { label: DEFAULT_LABELS[type] || type, config: defaultConfig },
    };

    nodesRef.current = [...nodesRef.current, newNode];
    setCanvasVersion((v) => v + 1);
    setHasChanges(true);
  }, []);

  const handleNodeSelect = useCallback((nodeKey: string | null) => {
    setSelectedNodeKey(nodeKey);
  }, []);

  const handleNodesChange = useCallback((nodes: Node[]) => {
    nodesRef.current = nodes;
    setHasChanges(true);
  }, []);

  const handleEdgesChange = useCallback((edges: Edge[]) => {
    edgesRef.current = edges;
    setHasChanges(true);
  }, []);

  const handleUpdateNode = useCallback((nodeKey: string, updates: Partial<WorkflowNode>) => {
    nodesRef.current = nodesRef.current.map((n) => {
      if (n.id !== nodeKey) return n;
      const data = { ...(n.data as any) };
      if (updates.display_name !== undefined) data.label = updates.display_name;
      if (updates.description !== undefined) data.description = updates.description;
      if (updates.config !== undefined) {
        data.config = updates.config;
        if (updates.config.agent_key) data.agent_key = updates.config.agent_key;
      }
      return { ...n, data };
    });
    setHasChanges(true);
    setNodeConfigVersion((v) => v + 1); // triggers useMemo re-derivation for properties panel
  }, []);

  const handleDeleteNode = useCallback((nodeKey: string) => {
    nodesRef.current = nodesRef.current.filter((n) => n.id !== nodeKey);
    edgesRef.current = edgesRef.current.filter((e) => e.source !== nodeKey && e.target !== nodeKey);
    setSelectedNodeKey(null);
    setHasChanges(true);
    setCanvasVersion((v) => v + 1);
  }, []);

  const loadExecutions = async () => {
    if (!workflow) return;
    try {
      const data = await getWorkflowExecutions(workflow.id, 20);
      setExecutions(data);
    } catch { /* ignore */ }
  };

  // Selected node data for properties panel
  const selectedNode: WorkflowNode | null = useMemo(() => {
    if (!selectedNodeKey) return null;
    const n = nodesRef.current.find((n) => n.id === selectedNodeKey);
    if (!n) return null;
    return {
      node_key: n.id,
      node_type: (n.type || 'agent') as NodeType,
      display_name: (n.data as any)?.label,
      description: (n.data as any)?.description,
      position: n.position,
      config: (n.data as any)?.config || {},
    };
  }, [selectedNodeKey, nodeConfigVersion]);

  if (loading) {
    return (
      <div className="h-[calc(100vh-64px)] flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin w-8 h-8 border-2 border-accent border-t-transparent rounded-full mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">Loading workflow...</p>
        </div>
      </div>
    );
  }

  if (!workflow) {
    return (
      <div className="h-[calc(100vh-64px)] flex items-center justify-center">
        <div className="text-center">
          <AlertCircle size={32} className="mx-auto text-red-300 mb-2" />
          <p className="text-sm text-red-400">Workflow not found</p>
          <button onClick={() => navigate('/admin/workflows')} className="mt-3 text-xs text-accent hover:underline">
            Back to workflows
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-background">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 bg-card border-b border-border shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/admin/workflows')}
            className="p-1.5 rounded-md text-muted-foreground hover:text-muted-foreground hover:bg-muted transition-colors"
          >
            <ArrowLeft size={16} />
          </button>
          <div>
            <h1 className="text-sm font-semibold text-foreground flex items-center gap-2">
              <GitBranch size={14} className="text-accent" />
              {workflow.display_name}
            </h1>
            <div className="flex items-center gap-2 text-[10px] text-muted-foreground mt-0.5">
              <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full border text-[10px] font-medium ${
                workflow.status === 'published' ? 'bg-green-50 dark:bg-green-500/15 border-green-200 dark:border-green-500/20 text-green-700 dark:text-green-400' :
                workflow.status === 'archived' ? 'bg-muted border-border text-muted-foreground' :
                'bg-amber-50 dark:bg-amber-500/15 border-amber-200 dark:border-amber-500/20 text-amber-700 dark:text-amber-400'
              }`}>
                {workflow.status === 'published' ? <CheckCircle2 size={8} /> : <Clock size={8} />}
                {workflow.status}
              </span>
              <span>v{workflow.current_version}</span>
              {hasChanges && <span className="text-amber-500 font-medium animate-pulse">Unsaved changes</span>}
              {saveToast && (
                <span className={`font-medium ${saveToast === 'Save failed' ? 'text-red-500' : 'text-green-600 dark:text-green-400'}`}>
                  {saveToast}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {/* Visual / JSON toggle */}
          <div className="flex items-center gap-0.5 p-0.5 bg-muted rounded-md mr-1">
            <button
              onClick={() => setEditorMode('visual')}
              className={`inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded transition-colors ${
                editorMode === 'visual' ? 'bg-card shadow-sm text-foreground font-medium' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <Eye size={12} />Visual
            </button>
            <button
              onClick={() => setEditorMode('json')}
              className={`inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded transition-colors ${
                editorMode === 'json' ? 'bg-card shadow-sm text-foreground font-medium' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <Code size={12} />JSON
            </button>
          </div>
          <button
            onClick={() => { setShowExecPanel(!showExecPanel); setBottomTab('test'); }}
            className={`inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium border rounded-md transition-colors ${
              showExecPanel ? 'bg-accent/10 border-accent/20 text-accent' : 'border-border text-muted-foreground hover:bg-muted/40'
            }`}
            title="Test & Execute"
          >
            <Play size={12} /> Test
          </button>
          <button
            onClick={handleValidate}
            className="px-2.5 py-1.5 text-xs font-medium border border-border rounded-md text-muted-foreground hover:bg-muted/40 transition-colors"
          >
            Validate
          </button>
          <button
            onClick={handleSave}
            disabled={saving || !hasChanges}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-accent/20 rounded-md text-accent bg-accent/10 hover:bg-accent/15 disabled:opacity-40 transition-colors"
          >
            <Save size={12} />
            {saving ? 'Saving...' : 'Save'}
          </button>
          <button
            onClick={handlePublish}
            disabled={publishing}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-accent text-accent-foreground hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            <Upload size={12} />
            {publishing ? 'Publishing...' : 'Publish'}
          </button>
        </div>
      </div>

      {/* Validation banner */}
      {validation && (
        <div className={`px-4 py-2 text-xs shrink-0 flex items-start gap-2 ${validation.valid ? 'bg-green-50 dark:bg-green-500/15 text-green-700 dark:text-green-400 border-b border-green-200 dark:border-green-500/20' : 'bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 border-b border-red-200 dark:border-red-500/20'}`}>
          <div className="flex-1">
            <div className="flex items-center gap-2 font-medium">
              {validation.valid ? (
                <><CheckCircle2 size={12} /> Workflow is valid</>
              ) : (
                <><AlertCircle size={12} /> {validation.errors.length} error(s) found</>
              )}
            </div>
            {validation.errors.map((e, i) => (
              <div key={i} className="ml-5 mt-0.5">- {e}</div>
            ))}
            {validation.warnings.map((w, i) => (
              <div key={`w-${i}`} className="ml-5 mt-0.5 text-amber-600 dark:text-amber-400">- {w}</div>
            ))}
          </div>
          <button onClick={() => setValidation(null)} className="p-0.5 hover:bg-card/50 rounded">
            <X size={12} />
          </button>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {editorMode === 'visual' ? (
          <>
            <WorkflowToolbox onAddNode={handleAddNode} />
            <WorkflowCanvas
              key={`canvas-${workflowId}-${canvasVersion}`}
              initialNodes={nodesRef.current}
              initialEdges={edgesRef.current}
              onNodesChange={handleNodesChange}
              onEdgesChange={handleEdgesChange}
              onNodeSelect={handleNodeSelect}
              onDrop={handleAddNode}
              activeNodeKey={activeTestNode}
            />
            <WorkflowPropertiesPanel
              selectedNode={selectedNode}
              availableAgents={agents}
              onUpdateNode={handleUpdateNode}
              onDeleteNode={handleDeleteNode}
            />
          </>
        ) : (
          <div className="flex-1 p-4 overflow-auto">
            <JsonEditor
              nodes={nodesRef.current}
              edges={edgesRef.current}
              onApply={(nodes, edges) => {
                nodesRef.current = nodes;
                edgesRef.current = edges;
                setHasChanges(true);
                setCanvasVersion((v) => v + 1);
              }}
            />
          </div>
        )}
      </div>

      {/* Bottom Panel: Test Runner + Execution History */}
      {showExecPanel && (
        <div className="h-80 shrink-0 border-t border-border bg-card flex flex-col">
          {/* Tab bar */}
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-border shrink-0">
            <div className="flex items-center gap-0.5">
              <button
                onClick={() => setBottomTab('test')}
                className={`inline-flex items-center gap-1.5 px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                  bottomTab === 'test' ? 'bg-accent/10 text-accent' : 'text-muted-foreground hover:text-foreground hover:bg-muted/40'
                }`}
              >
                <Play size={10} /> Test
              </button>
              <button
                onClick={() => { setBottomTab('history'); loadExecutions(); }}
                className={`inline-flex items-center gap-1.5 px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                  bottomTab === 'history' ? 'bg-accent/10 text-accent' : 'text-muted-foreground hover:text-foreground hover:bg-muted/40'
                }`}
              >
                <History size={10} /> History
              </button>
            </div>
            <button onClick={() => setShowExecPanel(false)} className="p-0.5 text-muted-foreground hover:text-muted-foreground">
              <X size={12} />
            </button>
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-hidden">
            {bottomTab === 'test' ? (
              <WorkflowTestRunner
                workflowId={workflow.id}
                onActiveNode={setActiveTestNode}
              />
            ) : (
              <div className="overflow-auto h-full">
                {executions.length === 0 ? (
                  <div className="text-center py-6 text-xs text-muted-foreground">No executions yet</div>
                ) : (
                  <table className="w-full text-xs">
                    <thead className="bg-muted/40 sticky top-0">
                      <tr>
                        <th className="text-left px-4 py-1.5 font-medium text-muted-foreground">ID</th>
                        <th className="text-left px-4 py-1.5 font-medium text-muted-foreground">Status</th>
                        <th className="text-left px-4 py-1.5 font-medium text-muted-foreground">Trigger</th>
                        <th className="text-left px-4 py-1.5 font-medium text-muted-foreground">Nodes</th>
                        <th className="text-left px-4 py-1.5 font-medium text-muted-foreground">Latency</th>
                        <th className="text-left px-4 py-1.5 font-medium text-muted-foreground">Date</th>
                      </tr>
                    </thead>
                    <tbody>
                      {executions.map((ex: any) => (
                        <tr key={ex.id} className="border-t border-border hover:bg-muted/40">
                          <td className="px-4 py-1.5 font-mono text-muted-foreground">#{ex.id}</td>
                          <td className="px-4 py-1.5">
                            <span className={`inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium ${
                              ex.status === 'completed' ? 'bg-green-50 dark:bg-green-500/15 text-green-700 dark:text-green-400' :
                              ex.status === 'failed' ? 'bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400' :
                              ex.status === 'paused' ? 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-400' :
                              'bg-accent/10 text-accent'
                            }`}>
                              {ex.status}
                            </span>
                          </td>
                          <td className="px-4 py-1.5 text-muted-foreground">{ex.trigger}</td>
                          <td className="px-4 py-1.5 text-muted-foreground">{ex.execution_path?.length || 0}</td>
                          <td className="px-4 py-1.5 text-muted-foreground">{ex.total_latency_ms ? `${ex.total_latency_ms}ms` : '-'}</td>
                          <td className="px-4 py-1.5 text-muted-foreground">{ex.created_at ? new Date(ex.created_at).toLocaleString() : '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── JSON Editor ──────────────────────────────────────────────── */

function JsonEditor({
  nodes,
  edges,
  onApply,
}: {
  nodes: Node[];
  edges: Edge[];
  onApply: (nodes: Node[], edges: Edge[]) => void;
}) {
  const graphData = {
    nodes: fromReactFlowNodes(nodes),
    edges: fromReactFlowEdges(edges),
  };
  const [raw, setRaw] = useState(JSON.stringify(graphData, null, 2));
  const [error, setError] = useState<string | null>(null);

  const apply = () => {
    try {
      const parsed = JSON.parse(raw);
      setError(null);
      const rfNodes = toReactFlowNodes(parsed.nodes || []);
      const rfEdges = toReactFlowEdges(parsed.edges || []);
      onApply(rfNodes, rfEdges);
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="space-y-3 max-w-4xl mx-auto">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">JSON Graph Editor</h3>
        <button
          onClick={apply}
          className="px-4 py-1.5 text-sm font-medium bg-accent text-accent-foreground rounded-md hover:bg-accent/90 transition-colors"
        >
          Apply Changes
        </button>
      </div>
      {error && <p className="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15 p-2 rounded-md border border-red-200 dark:border-red-500/20">JSON Error: {error}</p>}
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        className="w-full h-[calc(100vh-250px)] font-mono text-xs p-4 border border-border rounded-lg bg-card focus:outline-none focus:ring-2 focus:ring-ring"
        spellCheck={false}
      />
    </div>
  );
}
