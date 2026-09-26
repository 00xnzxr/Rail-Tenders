import { useState, useEffect, useCallback } from 'react';
import {
  FileText, Database, Globe, Calculator, Brain, Server, Code,
  Search, ChevronDown, ChevronRight, X, Wrench, Play, Loader2,
  CheckCircle2, XCircle,
} from 'lucide-react';
import { getBuilderTools, testBuilderTool } from '../../lib/api';

interface BuilderTool {
  id: number;
  tool_key: string;
  display_name: string;
  description: string;
  tool_type: string;
  config_schema: Record<string, any>;
  default_config: Record<string, any>;
  is_system: boolean;
  is_active: boolean;
}

interface AssignedTool {
  tool_id: number;
  config: Record<string, any>;
}

interface ToolConfigPanelProps {
  assignedTools: AssignedTool[];
  onChange: (tools: AssignedTool[]) => void;
}

interface TestState {
  input: string;
  output: string | null;
  latencyMs: number | null;
  status: 'idle' | 'running' | 'success' | 'error';
}

const TOOL_TYPE_META: Record<string, { label: string; icon: React.ElementType }> = {
  document_op: { label: 'Document Operations', icon: FileText },
  db_query: { label: 'Database Queries', icon: Database },
  web_search: { label: 'Web & External', icon: Globe },
  calculation: { label: 'Calculation', icon: Calculator },
  memory: { label: 'Memory', icon: Brain },
  mcp: { label: 'External (MCP)', icon: Server },
  code_exec: { label: 'Code Execution', icon: Code },
};

const TYPE_BADGE_COLORS: Record<string, string> = {
  document_op: 'bg-accent/15 text-accent',
  db_query: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  web_search: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  calculation: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
  memory: 'bg-pink-100 dark:bg-pink-500/20 text-pink-700 dark:text-pink-400',
  mcp: 'bg-muted text-muted-foreground',
  code_exec: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-400',
};

function getToolIcon(toolType: string) {
  return TOOL_TYPE_META[toolType]?.icon || Wrench;
}

function renderConfigField(
  key: string,
  schema: Record<string, any>,
  value: any,
  onChangeValue: (key: string, val: any) => void,
) {
  const prop = schema.properties?.[key];
  if (!prop) return null;

  const label = prop.title || key;
  const desc = prop.description;

  if (prop.type === 'boolean') {
    return (
      <label key={key} className="flex items-center gap-2 text-sm text-foreground cursor-pointer">
        <input
          type="checkbox"
          checked={!!value}
          onChange={(e) => onChangeValue(key, e.target.checked)}
          className="rounded border-border text-accent focus:ring-ring"
        />
        <span>{label}</span>
        {desc && <span className="text-xs text-muted-foreground ml-1">({desc})</span>}
      </label>
    );
  }

  if (prop.type === 'string' && prop.enum) {
    return (
      <div key={key} className="space-y-1">
        <label className="text-xs font-medium text-muted-foreground">{label}</label>
        <select
          value={value ?? ''}
          onChange={(e) => onChangeValue(key, e.target.value)}
          className="w-full border border-border rounded-md px-2 py-1.5 text-sm bg-card focus:ring-1 focus:ring-ring focus:border-ring"
        >
          {prop.enum.map((opt: string) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
      </div>
    );
  }

  if (prop.type === 'integer' || prop.type === 'number') {
    return (
      <div key={key} className="space-y-1">
        <label className="text-xs font-medium text-muted-foreground">{label}</label>
        <input
          type="number"
          value={value ?? ''}
          step={prop.type === 'integer' ? 1 : 'any'}
          min={prop.minimum}
          max={prop.maximum}
          onChange={(e) => onChangeValue(key, prop.type === 'integer' ? parseInt(e.target.value) : parseFloat(e.target.value))}
          className="w-full border border-border rounded-md px-2 py-1.5 text-sm focus:ring-1 focus:ring-ring focus:border-ring"
        />
        {desc && <p className="text-xs text-muted-foreground">{desc}</p>}
      </div>
    );
  }

  if (prop.type === 'array') {
    return (
      <div key={key} className="space-y-1">
        <label className="text-xs font-medium text-muted-foreground">{label}</label>
        <input
          type="text"
          value={Array.isArray(value) ? value.join(', ') : value ?? ''}
          onChange={(e) => onChangeValue(key, e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean))}
          placeholder="comma-separated values"
          className="w-full border border-border rounded-md px-2 py-1.5 text-sm focus:ring-1 focus:ring-ring focus:border-ring"
        />
        {desc && <p className="text-xs text-muted-foreground">{desc}</p>}
      </div>
    );
  }

  // Default: string input
  return (
    <div key={key} className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">{label}</label>
      <input
        type="text"
        value={value ?? ''}
        onChange={(e) => onChangeValue(key, e.target.value)}
        className="w-full border border-border rounded-md px-2 py-1.5 text-sm focus:ring-1 focus:ring-ring focus:border-ring"
      />
      {desc && <p className="text-xs text-muted-foreground">{desc}</p>}
    </div>
  );
}

export default function ToolConfigPanel({ assignedTools, onChange }: ToolConfigPanelProps) {
  const [allTools, setAllTools] = useState<BuilderTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const [testStates, setTestStates] = useState<Record<number, TestState>>({});

  useEffect(() => {
    getBuilderTools()
      .then((tools) => setAllTools(tools))
      .finally(() => setLoading(false));
  }, []);

  const assignedSet = new Set(assignedTools.map((t) => t.tool_id));

  const toggleAssigned = useCallback(
    (tool: BuilderTool) => {
      if (assignedSet.has(tool.id)) {
        onChange(assignedTools.filter((t) => t.tool_id !== tool.id));
      } else {
        onChange([...assignedTools, { tool_id: tool.id, config: { ...(tool.default_config || {}) } }]);
      }
    },
    [assignedTools, assignedSet, onChange],
  );

  const removeTool = useCallback(
    (toolId: number) => {
      onChange(assignedTools.filter((t) => t.tool_id !== toolId));
    },
    [assignedTools, onChange],
  );

  const updateConfig = useCallback(
    (toolId: number, key: string, value: any) => {
      onChange(
        assignedTools.map((t) =>
          t.tool_id === toolId ? { ...t, config: { ...t.config, [key]: value } } : t,
        ),
      );
    },
    [assignedTools, onChange],
  );

  const toggleExpanded = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const runTest = async (toolId: number) => {
    const input = testStates[toolId]?.input || '';
    setTestStates((s) => ({ ...s, [toolId]: { ...s[toolId], input, status: 'running', output: null, latencyMs: null } }));
    try {
      const result = await testBuilderTool(toolId, input);
      setTestStates((s) => ({
        ...s,
        [toolId]: { input, output: result.output, latencyMs: result.latency_ms, status: result.status === 'error' ? 'error' : 'success' },
      }));
    } catch {
      setTestStates((s) => ({
        ...s,
        [toolId]: { ...s[toolId], status: 'error', output: 'Test request failed', latencyMs: null },
      }));
    }
  };

  // Filter tools by search
  const filtered = allTools.filter(
    (t) =>
      t.is_active &&
      (searchQuery === '' ||
        t.display_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        t.description.toLowerCase().includes(searchQuery.toLowerCase()) ||
        t.tool_key.toLowerCase().includes(searchQuery.toLowerCase())),
  );

  // Group by type
  const grouped: Record<string, BuilderTool[]> = {};
  for (const tool of filtered) {
    const type = tool.tool_type;
    if (!grouped[type]) grouped[type] = [];
    grouped[type].push(tool);
  }

  const toolMap = new Map(allTools.map((t) => [t.id, t]));

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 size={20} className="animate-spin mr-2" />
        Loading tools...
      </div>
    );
  }

  return (
    <div className="flex gap-4 h-full min-h-[480px]">
      {/* Left Panel — Available Tools */}
      <div className="w-2/5 border border-border rounded-lg bg-card flex flex-col">
        <div className="p-3 border-b border-border">
          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search tools..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-8 pr-3 py-1.5 border border-border rounded-md text-sm focus:ring-1 focus:ring-ring focus:border-ring"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-3">
          {Object.entries(TOOL_TYPE_META).map(([type, meta]) => {
            const tools = grouped[type];
            if (!tools || tools.length === 0) return null;
            const Icon = meta.icon;

            return (
              <div key={type}>
                <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide flex items-center gap-1.5 px-1 mb-1.5">
                  <Icon size={12} />
                  {meta.label}
                </h4>
                <div className="space-y-1">
                  {tools.map((tool) => {
                    const ToolIcon = getToolIcon(tool.tool_type);
                    return (
                      <label
                        key={tool.id}
                        className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-muted/40 cursor-pointer transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={assignedSet.has(tool.id)}
                          onChange={() => toggleAssigned(tool)}
                          className="rounded border-border text-accent focus:ring-ring"
                        />
                        <ToolIcon size={14} className="text-muted-foreground shrink-0" />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="text-sm font-medium text-foreground truncate">{tool.display_name}</span>
                            <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0 ${TYPE_BADGE_COLORS[tool.tool_type] || 'bg-muted text-muted-foreground'}`}>
                              {tool.tool_type}
                            </span>
                          </div>
                          <p className="text-xs text-muted-foreground truncate">{tool.description}</p>
                        </div>
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })}

          {filtered.length === 0 && (
            <div className="text-center py-6 text-muted-foreground text-sm">
              <Search size={20} className="mx-auto mb-1 opacity-50" />
              No tools found
            </div>
          )}
        </div>
      </div>

      {/* Right Panel — Assigned Tools */}
      <div className="w-3/5 border border-border rounded-lg bg-card flex flex-col">
        <div className="p-3 border-b border-border">
          <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
            <Wrench size={14} />
            Assigned Tools ({assignedTools.length})
          </h3>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {assignedTools.length === 0 && (
            <div className="text-center py-10 text-muted-foreground text-sm">
              <Wrench size={24} className="mx-auto mb-2 opacity-50" />
              <p>No tools assigned yet</p>
              <p className="text-xs mt-1">Select tools from the left panel</p>
            </div>
          )}

          {assignedTools.map((assigned, idx) => {
            const tool = toolMap.get(assigned.tool_id);
            if (!tool) return null;

            const Icon = getToolIcon(tool.tool_type);
            const isExpanded = expandedIds.has(tool.id);
            const test = testStates[tool.id] || { input: '', output: null, latencyMs: null, status: 'idle' };
            const schemaProps = tool.config_schema?.properties || {};
            const configKeys = Object.keys(schemaProps);
            const configPreview = configKeys
              .slice(0, 3)
              .map((k) => `${k}: ${JSON.stringify(assigned.config[k] ?? (tool.default_config || {})[k]) ?? '-'}`)
              .join(', ');

            return (
              <div key={assigned.tool_id} className="border border-border rounded-lg bg-muted/40">
                {/* Card header */}
                <div className="flex items-center gap-2 px-3 py-2">
                  <button onClick={() => toggleExpanded(tool.id)} className="text-muted-foreground hover:text-muted-foreground">
                    {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>
                  <span className="text-xs font-mono text-muted-foreground w-5 text-right">{idx + 1}.</span>
                  <Icon size={14} className="text-accent shrink-0" />
                  <span className="text-sm font-medium text-foreground">{tool.display_name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${TYPE_BADGE_COLORS[tool.tool_type] || 'bg-muted text-muted-foreground'}`}>
                    {tool.tool_type}
                  </span>
                  <button
                    onClick={() => removeTool(tool.id)}
                    className="ml-auto text-muted-foreground/50 hover:text-red-500 transition-colors p-0.5"
                    title="Remove tool"
                  >
                    <X size={14} />
                  </button>
                </div>

                {/* Collapsed preview */}
                {!isExpanded && configKeys.length > 0 && (
                  <div className="px-3 pb-2">
                    <p className="text-xs text-muted-foreground truncate">{configPreview}</p>
                  </div>
                )}

                {/* Expanded config */}
                {isExpanded && (
                  <div className="px-3 pb-3 space-y-3 border-t border-border mt-1 pt-3">
                    {configKeys.length > 0 ? (
                      <div className="space-y-2.5">
                        {configKeys.map((key) =>
                          renderConfigField(
                            key,
                            tool.config_schema,
                            assigned.config[key] ?? (tool.default_config || {})[key],
                            (k, v) => updateConfig(tool.id, k, v),
                          ),
                        )}
                      </div>
                    ) : (
                      <p className="text-xs text-muted-foreground italic">No configuration options</p>
                    )}

                    {/* Test Tool Section */}
                    <div className="border-t border-border pt-2.5 space-y-2">
                      <p className="text-xs font-semibold text-muted-foreground">Test Tool</p>
                      <div className="flex gap-2">
                        <input
                          type="text"
                          placeholder="Enter test input..."
                          value={test.input}
                          onChange={(e) =>
                            setTestStates((s) => ({
                              ...s,
                              [tool.id]: { ...test, input: e.target.value },
                            }))
                          }
                          className="flex-1 border border-border rounded-md px-2 py-1.5 text-sm focus:ring-1 focus:ring-ring focus:border-ring"
                        />
                        <button
                          onClick={() => runTest(tool.id)}
                          disabled={test.status === 'running'}
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-accent text-accent-foreground text-sm rounded-md hover:bg-accent/90 disabled:opacity-50 transition-colors"
                        >
                          {test.status === 'running' ? (
                            <Loader2 size={13} className="animate-spin" />
                          ) : (
                            <Play size={13} />
                          )}
                          Run
                        </button>
                      </div>

                      {test.output !== null && (
                        <div className="bg-card border border-border rounded-md p-2 space-y-1">
                          <div className="flex items-center gap-2 text-xs">
                            {test.status === 'success' ? (
                              <CheckCircle2 size={13} className="text-green-500" />
                            ) : (
                              <XCircle size={13} className="text-red-500" />
                            )}
                            <span className={test.status === 'success' ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}>
                              {test.status === 'success' ? 'Success' : 'Error'}
                            </span>
                            {test.latencyMs !== null && (
                              <span className="text-muted-foreground ml-auto">{test.latencyMs}ms</span>
                            )}
                          </div>
                          <pre className="text-xs text-muted-foreground bg-muted/40 rounded p-2 overflow-x-auto max-h-32 whitespace-pre-wrap">
                            {typeof test.output === 'string' ? test.output : JSON.stringify(test.output, null, 2)}
                          </pre>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
