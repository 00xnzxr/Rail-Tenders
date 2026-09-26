import { useState, useEffect } from 'react';
import { Settings2, Trash2 } from 'lucide-react';
import type { WorkflowNode } from '../../types/workflow';

interface WorkflowPropertiesPanelProps {
  selectedNode: WorkflowNode | null;
  availableAgents: Array<{ agent_key: string; display_name: string; id: number }>;
  onUpdateNode: (nodeKey: string, updates: Partial<WorkflowNode>) => void;
  onDeleteNode: (nodeKey: string) => void;
}

export default function WorkflowPropertiesPanel({
  selectedNode,
  availableAgents,
  onUpdateNode,
  onDeleteNode,
}: WorkflowPropertiesPanelProps) {
  if (!selectedNode) {
    return (
      <div className="w-72 shrink-0 bg-card border-l border-border flex items-center justify-center">
        <div className="text-center text-muted-foreground px-6">
          <Settings2 size={24} className="mx-auto mb-2 text-muted-foreground/50" />
          <p className="text-xs">Select a node to configure</p>
        </div>
      </div>
    );
  }

  const config = selectedNode.config || {};
  const canDelete = selectedNode.node_type !== 'start' && selectedNode.node_type !== 'end';

  const updateConfig = (key: string, value: any) => {
    if (key === '__batch__' && typeof value === 'object') {
      // Batch update: merge multiple config keys in a single call
      onUpdateNode(selectedNode.node_key, {
        config: { ...config, ...value },
      });
    } else {
      onUpdateNode(selectedNode.node_key, {
        config: { ...config, [key]: value },
      });
    }
  };

  return (
    <div className="w-72 shrink-0 bg-card border-l border-border overflow-y-auto">
      <div className="p-4">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-foreground">Node Properties</h3>
          {canDelete && (
            <button
              onClick={() => onDeleteNode(selectedNode.node_key)}
              className="p-1 rounded text-muted-foreground hover:text-red-500 hover:bg-red-50 dark:bg-red-500/15 transition-colors"
              title="Delete node"
            >
              <Trash2 size={14} />
            </button>
          )}
        </div>

        {/* Common fields */}
        <div className="space-y-3 mb-4">
          <Field label="Display Name">
            <input
              value={selectedNode.display_name || ''}
              onChange={(e) => onUpdateNode(selectedNode.node_key, { display_name: e.target.value })}
              className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </Field>
          <Field label="Description">
            <textarea
              value={selectedNode.description || ''}
              onChange={(e) => onUpdateNode(selectedNode.node_key, { description: e.target.value })}
              rows={2}
              className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring resize-none"
            />
          </Field>
          <div className="text-[10px] text-muted-foreground">
            Type: <span className="font-mono">{selectedNode.node_type}</span> | Key: <span className="font-mono">{selectedNode.node_key}</span>
          </div>
        </div>

        <hr className="border-border mb-4" />

        {/* Type-specific config */}
        {selectedNode.node_type === 'agent' && (
          <AgentConfig config={config} agents={availableAgents} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'classify' && (
          <ClassifyConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'if_else' && (
          <IfElseConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'while_loop' && (
          <WhileLoopConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'user_approval' && (
          <UserApprovalConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'transform' && (
          <TransformConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'set_state' && (
          <SetStateConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'tool' && (
          <ToolConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'note' && (
          <NoteConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'for_each' && (
          <ForEachConfig config={config} updateConfig={updateConfig} />
        )}
        {selectedNode.node_type === 'parallel' && (
          <ParallelConfig config={config} updateConfig={updateConfig} />
        )}
      </div>
    </div>
  );
}

/* ─── Shared field wrapper ─────────────────────────────────────── */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-muted-foreground mb-1">{label}</label>
      {children}
    </div>
  );
}

/* ─── Agent Config ─────────────────────────────────────────────── */

function AgentConfig({
  config,
  agents,
  updateConfig,
}: {
  config: any;
  agents: Array<{ agent_key: string; display_name: string; id: number }>;
  updateConfig: (key: string, value: any) => void;
}) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Agent Configuration</h4>
      <Field label="Agent">
        <select
          value={config.agent_key || ''}
          onChange={(e) => {
            const agent = agents.find((a) => a.agent_key === e.target.value);
            // Batch both agent_key and agent_id in a single update to prevent clobbering
            updateConfig('__batch__', {
              agent_key: e.target.value,
              agent_id: agent?.id ?? null,
            });
          }}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        >
          <option value="">Select an agent...</option>
          {agents.map((a) => (
            <option key={a.agent_key} value={a.agent_key}>{a.display_name}</option>
          ))}
        </select>
      </Field>
      <Field label="Output Variable Key">
        <input
          value={config.output_key || ''}
          onChange={(e) => updateConfig('output_key', e.target.value)}
          placeholder="e.g., analysis_result"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}

/* ─── Classify Config ──────────────────────────────────────────── */

function ClassifyConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  const branches: Array<{ label: string; condition?: string }> = config.branches || [];

  const addBranch = () => {
    updateConfig('branches', [...branches, { label: `branch_${branches.length + 1}`, condition: '' }]);
  };

  const removeBranch = (i: number) => {
    updateConfig('branches', branches.filter((_, idx) => idx !== i));
  };

  const updateBranch = (i: number, field: string, value: string) => {
    const updated = [...branches];
    updated[i] = { ...updated[i], [field]: value };
    updateConfig('branches', updated);
  };

  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Classify / Router</h4>
      <Field label="Classification Prompt">
        <textarea
          value={config.classification_prompt || ''}
          onChange={(e) => updateConfig('classification_prompt', e.target.value)}
          rows={4}
          placeholder="Describe how to classify the input..."
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring resize-none"
        />
      </Field>
      <Field label="Model (optional)">
        <input
          value={config.model || ''}
          onChange={(e) => updateConfig('model', e.target.value)}
          placeholder="claude-sonnet-4-6"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Branches">
        <div className="space-y-1.5">
          {branches.map((b, i) => (
            <div key={i} className="flex items-center gap-1">
              <input
                value={b.label}
                onChange={(e) => updateBranch(i, 'label', e.target.value)}
                className="flex-1 px-2 py-1 border border-border rounded text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
                placeholder="branch label"
              />
              <button
                onClick={() => removeBranch(i)}
                className="p-1 text-muted-foreground hover:text-red-500"
              >
                <Trash2 size={10} />
              </button>
            </div>
          ))}
          <button onClick={addBranch} className="text-xs text-accent hover:text-accent/80 font-medium">
            + Add Branch
          </button>
        </div>
      </Field>
    </div>
  );
}

/* ─── If/Else Config ───────────────────────────────────────────── */

function IfElseConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Condition</h4>
      <Field label="Condition Expression">
        <textarea
          value={config.condition_expression || ''}
          onChange={(e) => updateConfig('condition_expression', e.target.value)}
          rows={2}
          placeholder='e.g., state.has_analysis == True'
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring resize-none"
        />
      </Field>
      <Field label="True Label">
        <input
          value={config.true_label || 'True'}
          onChange={(e) => updateConfig('true_label', e.target.value)}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="False Label">
        <input
          value={config.false_label || 'False'}
          onChange={(e) => updateConfig('false_label', e.target.value)}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}

/* ─── While Loop Config ────────────────────────────────────────── */

function WhileLoopConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Loop Configuration</h4>
      <Field label="Condition Expression">
        <textarea
          value={config.condition_expression || ''}
          onChange={(e) => updateConfig('condition_expression', e.target.value)}
          rows={2}
          placeholder='e.g., state.iteration < 3'
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring resize-none"
        />
      </Field>
      <Field label="Max Iterations">
        <input
          type="number"
          value={config.max_iterations || 10}
          onChange={(e) => updateConfig('max_iterations', parseInt(e.target.value) || 10)}
          min={1}
          max={100}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}

/* ─── User Approval Config ─────────────────────────────────────── */

function UserApprovalConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Approval Gate</h4>
      <Field label="Prompt Template">
        <textarea
          value={config.prompt_template || ''}
          onChange={(e) => updateConfig('prompt_template', e.target.value)}
          rows={3}
          placeholder="Approve this step?"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring resize-none"
        />
      </Field>
      <Field label="Timeout (seconds)">
        <input
          type="number"
          value={config.timeout_seconds || 3600}
          onChange={(e) => updateConfig('timeout_seconds', parseInt(e.target.value) || 3600)}
          min={60}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}

/* ─── Transform Config ─────────────────────────────────────────── */

function TransformConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Data Transform</h4>
      <Field label="Transform Type">
        <select
          value={config.transform_type || 'jinja2'}
          onChange={(e) => updateConfig('transform_type', e.target.value)}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        >
          <option value="jinja2">Jinja2 Template</option>
          <option value="expression">Expression</option>
        </select>
      </Field>
      <Field label="Template / Expression">
        <textarea
          value={config.template || ''}
          onChange={(e) => updateConfig('template', e.target.value)}
          rows={4}
          placeholder='{{ state.analysis_result.output }}'
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring resize-none"
        />
      </Field>
      <Field label="Output Variable Key">
        <input
          value={config.output_key || ''}
          onChange={(e) => updateConfig('output_key', e.target.value)}
          placeholder="formatted_output"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}

/* ─── Set State Config ─────────────────────────────────────────── */

function SetStateConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  const assignments: Array<{ key: string; value_expression: string }> = config.assignments || [];

  const addAssignment = () => {
    updateConfig('assignments', [...assignments, { key: '', value_expression: '' }]);
  };

  const removeAssignment = (i: number) => {
    updateConfig('assignments', assignments.filter((_, idx) => idx !== i));
  };

  const updateAssignment = (i: number, field: string, value: string) => {
    const updated = [...assignments];
    updated[i] = { ...updated[i], [field]: value };
    updateConfig('assignments', updated);
  };

  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Variable Assignments</h4>
      <div className="space-y-2">
        {assignments.map((a, i) => (
          <div key={i} className="flex items-start gap-1">
            <div className="flex-1 space-y-1">
              <input
                value={a.key}
                onChange={(e) => updateAssignment(i, 'key', e.target.value)}
                placeholder="variable_name"
                className="w-full px-2 py-1 border border-border rounded text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <input
                value={a.value_expression}
                onChange={(e) => updateAssignment(i, 'value_expression', e.target.value)}
                placeholder="value or expression"
                className="w-full px-2 py-1 border border-border rounded text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
            <button onClick={() => removeAssignment(i)} className="p-1 mt-0.5 text-muted-foreground hover:text-red-500">
              <Trash2 size={10} />
            </button>
          </div>
        ))}
        <button onClick={addAssignment} className="text-xs text-accent hover:text-accent/80 font-medium">
          + Add Assignment
        </button>
      </div>
    </div>
  );
}

/* ─── Tool Config ──────────────────────────────────────────────── */

function ToolConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Tool Configuration</h4>
      <Field label="Tool Key">
        <input
          value={config.tool_key || ''}
          onChange={(e) => updateConfig('tool_key', e.target.value)}
          placeholder="web_search"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Output Variable Key">
        <input
          value={config.output_key || ''}
          onChange={(e) => updateConfig('output_key', e.target.value)}
          placeholder="search_results"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}

/* ─── Note Config ──────────────────────────────────────────────── */

function NoteConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Note</h4>
      <Field label="Text">
        <textarea
          value={config.text || ''}
          onChange={(e) => updateConfig('text', e.target.value)}
          rows={4}
          placeholder="Add documentation or notes..."
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring resize-none"
        />
      </Field>
    </div>
  );
}

/* ─── For Each Config ──────────────────────────────────────────── */

function ForEachConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">For Each / Iteration</h4>
      <Field label="Collection Expression">
        <input
          value={config.collection_expression || ''}
          onChange={(e) => updateConfig('collection_expression', e.target.value)}
          placeholder="state.all_documents"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Item Variable">
        <input
          value={config.item_variable || 'current_item'}
          onChange={(e) => updateConfig('item_variable', e.target.value)}
          placeholder="current_document"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Index Variable">
        <input
          value={config.index_variable || 'current_index'}
          onChange={(e) => updateConfig('index_variable', e.target.value)}
          placeholder="current_index"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Output Key (accumulator)">
        <input
          value={config.output_key || ''}
          onChange={(e) => updateConfig('output_key', e.target.value)}
          placeholder="document_analyses"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Parallel Execution">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={config.parallel || false}
            onChange={(e) => updateConfig('parallel', e.target.checked)}
            className="rounded border-border text-teal-600 dark:text-teal-400 focus:ring-teal-300"
          />
          <span className="text-xs text-muted-foreground">Run iterations concurrently</span>
        </label>
      </Field>
      {config.parallel && (
        <Field label="Concurrency Limit">
          <input
            type="number"
            value={config.concurrency_limit || 3}
            onChange={(e) => updateConfig('concurrency_limit', parseInt(e.target.value) || 3)}
            min={1}
            max={10}
            className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </Field>
      )}
      <Field label="Max Iterations">
        <input
          type="number"
          value={config.max_iterations || 20}
          onChange={(e) => updateConfig('max_iterations', parseInt(e.target.value) || 20)}
          min={1}
          max={100}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Continue on Error">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={config.continue_on_error !== false}
            onChange={(e) => updateConfig('continue_on_error', e.target.checked)}
            className="rounded border-border text-teal-600 dark:text-teal-400 focus:ring-teal-300"
          />
          <span className="text-xs text-muted-foreground">Continue if an iteration fails</span>
        </label>
      </Field>
    </div>
  );
}

/* ─── Parallel Config ──────────────────────────────────────────── */

function ParallelConfig({ config, updateConfig }: { config: any; updateConfig: (k: string, v: any) => void }) {
  const branches: Array<{ label: string; target_node_key?: string }> = config.branches || [];

  const addBranch = () => {
    updateConfig('branches', [...branches, { label: `branch_${branches.length + 1}`, target_node_key: '' }]);
  };

  const removeBranch = (i: number) => {
    updateConfig('branches', branches.filter((_, idx) => idx !== i));
  };

  const updateBranch = (i: number, field: string, value: string) => {
    const updated = [...branches];
    updated[i] = { ...updated[i], [field]: value };
    updateConfig('branches', updated);
  };

  return (
    <div className="space-y-3">
      <h4 className="text-xs font-semibold text-muted-foreground uppercase">Parallel Execution</h4>
      <Field label="Branches">
        <div className="space-y-2">
          {branches.map((b, i) => (
            <div key={i} className="space-y-1 p-2 bg-muted/40 rounded-md border border-border">
              <div className="flex items-center gap-1">
                <input
                  value={b.label}
                  onChange={(e) => updateBranch(i, 'label', e.target.value)}
                  placeholder="Branch label"
                  className="flex-1 px-2 py-1 border border-border rounded text-xs focus:outline-none focus:ring-2 focus:ring-ring"
                />
                <button onClick={() => removeBranch(i)} className="p-1 text-muted-foreground hover:text-red-500">
                  <Trash2 size={10} />
                </button>
              </div>
              <input
                value={b.target_node_key || ''}
                onChange={(e) => updateBranch(i, 'target_node_key', e.target.value)}
                placeholder="Target node key"
                className="w-full px-2 py-1 border border-border rounded text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
          ))}
          <button onClick={addBranch} className="text-xs text-rose-600 dark:text-rose-400 hover:text-rose-700 dark:text-rose-400 font-medium">
            + Add Branch
          </button>
        </div>
      </Field>
      <Field label="Merge Strategy">
        <select
          value={config.merge_strategy || 'dict'}
          onChange={(e) => updateConfig('merge_strategy', e.target.value)}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        >
          <option value="dict">Dictionary (key=label, value=result)</option>
          <option value="list">Array of results</option>
        </select>
      </Field>
      <Field label="Output Key">
        <input
          value={config.output_key || ''}
          onChange={(e) => updateConfig('output_key', e.target.value)}
          placeholder="parallel_results"
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs font-mono focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
      <Field label="Timeout (seconds)">
        <input
          type="number"
          value={config.timeout_seconds || 300}
          onChange={(e) => updateConfig('timeout_seconds', parseInt(e.target.value) || 300)}
          min={30}
          className="w-full px-2.5 py-1.5 border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </Field>
    </div>
  );
}
