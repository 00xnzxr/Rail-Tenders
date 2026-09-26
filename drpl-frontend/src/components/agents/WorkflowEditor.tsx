import { useState, useCallback, useMemo } from 'react';
import { Bot, Wrench, GitBranch, Play, Code, Eye, Plus, Info } from 'lucide-react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  Handle,
  Position,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import AgentNodeGraph from './AgentNodeGraph';

interface WorkflowEditorProps {
  agentType: string;
  tools: Array<{ tool_id: number; tool_key: string; display_name: string }>;
  orchestrationConfig: any;
  onOrchestrationConfigChange: (config: any) => void;
  availableAgents: Array<{ agent_key: string; display_name: string }>;
}

/* ── Custom node components for the ReactFlow orchestrator canvas ── */

function AgentNode({ data }: any) {
  return (
    <div className="px-3 py-2 rounded-lg bg-card border-2 border-blue-400 shadow-sm min-w-[120px]">
      <Handle type="target" position={Position.Left} className="!bg-blue-400" />
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <Bot size={14} className="text-accent" />
        {data.label}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-blue-400" />
    </div>
  );
}

function ToolNode({ data }: any) {
  return (
    <div className="px-3 py-2 rounded-lg bg-card border-2 border-green-400 shadow-sm min-w-[120px]">
      <Handle type="target" position={Position.Left} className="!bg-green-400" />
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <Wrench size={14} className="text-green-500" />
        {data.label}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-green-400" />
    </div>
  );
}

function ConditionNode({ data }: any) {
  return (
    <div className="px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-500/15 border-2 border-amber-400 shadow-sm min-w-[120px] rotate-0"
      style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }}
    >
      <Handle type="target" position={Position.Left} className="!bg-amber-400" />
      <div className="flex items-center justify-center gap-1 text-xs font-medium text-foreground py-3">
        <GitBranch size={12} className="text-amber-600 dark:text-amber-400" />
        {data.label}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-amber-400" />
    </div>
  );
}

function InputNode() {
  return (
    <div className="px-4 py-2 rounded-lg bg-green-100 dark:bg-green-500/20 border border-green-300 shadow-sm">
      <div className="flex items-center gap-2 text-sm font-semibold text-green-800 dark:text-green-400">
        <Play size={14} /> Input
      </div>
      <Handle type="source" position={Position.Right} className="!bg-green-500" />
    </div>
  );
}

function OutputNode() {
  return (
    <div className="px-4 py-2 rounded-lg bg-purple-100 dark:bg-purple-500/20 border border-purple-300 shadow-sm">
      <Handle type="target" position={Position.Left} className="!bg-purple-500" />
      <div className="flex items-center gap-2 text-sm font-semibold text-purple-800 dark:text-purple-400">
        <Eye size={14} /> Output
      </div>
    </div>
  );
}

const nodeTypes = {
  agentNode: AgentNode,
  toolNode: ToolNode,
  conditionNode: ConditionNode,
  inputNode: InputNode,
  outputNode: OutputNode,
};

/* ── Info descriptions per agent type ── */

const INFO: Record<string, string> = {
  chain_of_thought:
    'This agent makes a single LLM call with no tool usage. The input is processed through the system prompt and model, producing a direct output.',
  react:
    'This agent uses the ReAct (Reasoning + Acting) pattern. It iteratively thinks, selects and calls tools, observes results, and repeats until it reaches a final answer or hits the iteration limit.',
  tool_use:
    'This agent uses the ReAct (Reasoning + Acting) pattern. It iteratively thinks, selects and calls tools, observes results, and repeats until it reaches a final answer or hits the iteration limit.',
};

/* ── Orchestrator visual editor ── */

function OrchestratorVisualEditor({
  orchestrationConfig,
  onOrchestrationConfigChange,
  availableAgents,
  tools,
}: Pick<WorkflowEditorProps, 'orchestrationConfig' | 'onOrchestrationConfigChange' | 'availableAgents' | 'tools'>) {
  const initialNodes = orchestrationConfig?.nodes ?? [];
  const initialEdges = orchestrationConfig?.edges ?? [];

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  const onConnect = useCallback(
    (params: any) => {
      const next = addEdge(params, edges);
      setEdges(next);
      onOrchestrationConfigChange({ ...orchestrationConfig, nodes, edges: next });
    },
    [edges, nodes, orchestrationConfig, onOrchestrationConfigChange, setEdges],
  );

  const pushConfig = useCallback(
    (n: any[], e: any[]) => onOrchestrationConfigChange({ ...orchestrationConfig, nodes: n, edges: e }),
    [orchestrationConfig, onOrchestrationConfigChange],
  );

  const handleNodesChange = useCallback(
    (changes: any) => {
      onNodesChange(changes);
      // Sync after state settles
      setTimeout(() => pushConfig(nodes, edges), 0);
    },
    [onNodesChange, pushConfig, nodes, edges],
  );

  const addNode = (type: string) => {
    const id = `node_${Date.now()}`;
    const labels: Record<string, string> = { agentNode: 'Agent', toolNode: 'Tool', conditionNode: 'Condition' };
    const newNode = {
      id,
      type,
      position: { x: 200 + Math.random() * 100, y: 150 + Math.random() * 80 },
      data: { label: labels[type] ?? type },
    };
    const next = [...nodes, newNode];
    setNodes(next);
    pushConfig(next, edges);
  };

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <button onClick={() => addNode('agentNode')} className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-accent/40 text-accent bg-accent/10 hover:bg-accent/15 transition-colors">
          <Plus size={12} /> Agent Node
        </button>
        <button onClick={() => addNode('toolNode')} className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-green-300 text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-500/15 hover:bg-green-100 dark:bg-green-500/20 transition-colors">
          <Plus size={12} /> Tool Node
        </button>
        <button onClick={() => addNode('conditionNode')} className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-amber-300 text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/15 hover:bg-amber-100 dark:bg-amber-500/20 transition-colors">
          <Plus size={12} /> Condition Node
        </button>
      </div>

      <div className="border border-border rounded-lg overflow-hidden" style={{ height: 500 }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={handleNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          nodeTypes={nodeTypes}
          fitView
        >
          <Background />
          <Controls />
          <MiniMap />
        </ReactFlow>
      </div>
    </div>
  );
}

/* ── JSON editor fallback ── */

function OrchestratorJsonEditor({
  orchestrationConfig,
  onOrchestrationConfigChange,
}: Pick<WorkflowEditorProps, 'orchestrationConfig' | 'onOrchestrationConfigChange'>) {
  const [raw, setRaw] = useState(() => JSON.stringify(orchestrationConfig ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);

  const apply = () => {
    try {
      const parsed = JSON.parse(raw);
      setError(null);
      onOrchestrationConfigChange(parsed);
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="space-y-2">
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        className="w-full h-72 font-mono text-xs p-3 border border-border rounded-lg bg-muted/40 focus:outline-none focus:ring-2 focus:ring-ring"
        spellCheck={false}
      />
      {error && <p className="text-xs text-red-600 dark:text-red-400">JSON Error: {error}</p>}
      <button onClick={apply} className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-md bg-accent text-accent-foreground hover:bg-accent/90 transition-colors">
        Apply
      </button>
    </div>
  );
}

/* ── Main export ── */

export default function WorkflowEditor({
  agentType,
  tools,
  orchestrationConfig,
  onOrchestrationConfigChange,
  availableAgents,
}: WorkflowEditorProps) {
  const [editorMode, setEditorMode] = useState<'visual' | 'json'>('visual');

  const toolsForGraph = useMemo(
    () => tools.map((t) => ({ tool_key: t.tool_key, display_name: t.display_name })),
    [tools],
  );

  /* Non-orchestrator types: read-only graph + info */
  if (agentType !== 'orchestrator') {
    const graphType = agentType as 'chain_of_thought' | 'react' | 'tool_use';
    return (
      <div className="space-y-4">
        <AgentNodeGraph agentType={graphType} tools={toolsForGraph} />
        {INFO[agentType] && (
          <div className="flex items-start gap-2.5 p-3 rounded-lg bg-accent/10 border border-accent/20 text-sm text-foreground">
            <Info size={16} className="text-accent mt-0.5 shrink-0" />
            <span>{INFO[agentType]}</span>
          </div>
        )}
      </div>
    );
  }

  /* Orchestrator: visual / json toggle */
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-1 p-1 bg-muted rounded-lg w-fit">
        <button
          onClick={() => setEditorMode('visual')}
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
            editorMode === 'visual' ? 'bg-card shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          <Eye size={14} /> Visual Editor
        </button>
        <button
          onClick={() => setEditorMode('json')}
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
            editorMode === 'json' ? 'bg-card shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          <Code size={14} /> JSON Editor
        </button>
      </div>

      {editorMode === 'visual' ? (
        <OrchestratorVisualEditor
          orchestrationConfig={orchestrationConfig}
          onOrchestrationConfigChange={onOrchestrationConfigChange}
          availableAgents={availableAgents}
          tools={tools}
        />
      ) : (
        <OrchestratorJsonEditor
          orchestrationConfig={orchestrationConfig}
          onOrchestrationConfigChange={onOrchestrationConfigChange}
        />
      )}
    </div>
  );
}
