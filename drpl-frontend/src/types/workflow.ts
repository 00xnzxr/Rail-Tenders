// Workflow Builder Types

export type NodeType =
  | 'start'
  | 'end'
  | 'agent'
  | 'classify'
  | 'if_else'
  | 'while_loop'
  | 'user_approval'
  | 'transform'
  | 'set_state'
  | 'tool'
  | 'note'
  | 'for_each'
  | 'parallel';

export interface WorkflowNode {
  id?: number;
  node_key: string;
  node_type: NodeType;
  display_name?: string;
  description?: string;
  position: { x: number; y: number };
  position_x?: number;
  position_y?: number;
  config: Record<string, any>;
  sort_order?: number;
}

export interface WorkflowEdge {
  id?: number;
  source: string;
  target: string;
  sourceHandle?: string;
  label?: string;
  condition?: string;
  sort_order?: number;
}

export interface Workflow {
  id: number;
  workflow_key: string;
  display_name: string;
  description?: string;
  category?: string;
  tags?: string[];
  current_version: number;
  status: 'draft' | 'published' | 'archived';
  is_default_router: boolean;
  trigger_type: string;
  input_schema?: any;
  output_schema?: any;
  variables_schema?: any;
  canvas_state?: any;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  created_by?: number;
  created_at?: string;
  updated_at?: string;
}

export interface WorkflowListItem {
  id: number;
  workflow_key: string;
  display_name: string;
  description?: string;
  category?: string;
  status: 'draft' | 'published' | 'archived';
  current_version: number;
  is_default_router: boolean;
  trigger_type: string;
  node_count: number;
  edge_count: number;
  execution_count: number;
  created_at?: string;
  updated_at?: string;
}

export interface WorkflowVersion {
  id: number;
  version_number: number;
  change_description?: string;
  created_by?: number;
  created_at?: string;
  node_count: number;
  edge_count: number;
}

export interface WorkflowNodeExecution {
  id: number;
  node_key: string;
  node_type: NodeType;
  status: 'running' | 'completed' | 'failed' | 'skipped';
  input_data?: any;
  output_data?: any;
  error_message?: string;
  latency_ms?: number;
  tokens_input?: number;
  tokens_output?: number;
  cost_estimate?: number;
  agent_execution_id?: number;
  started_at?: string;
  completed_at?: string;
}

export interface WorkflowExecution {
  id: number;
  workflow_id: number;
  workflow_version?: number;
  session_id?: string;
  trigger: string;
  status: 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
  current_node_key?: string;
  state_snapshot?: Record<string, any>;
  execution_path: string[];
  total_latency_ms?: number;
  total_tokens_input?: number;
  total_tokens_output?: number;
  total_cost?: number;
  error_message?: string;
  error_node_key?: string;
  pending_approval: boolean;
  approval_prompt?: string;
  input_data?: any;
  output_data?: any;
  user_id?: number;
  node_executions?: WorkflowNodeExecution[];
  created_at?: string;
  completed_at?: string;
}

export interface WorkflowValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

// Node config type helpers
export interface AgentNodeConfig {
  agent_id?: number;
  agent_key?: string;
  input_mapping?: Record<string, string>;
  output_key?: string;
}

export interface ClassifyNodeConfig {
  classification_prompt?: string;
  model?: string;
  branches: Array<{ label: string; condition?: string }>;
}

export interface IfElseNodeConfig {
  condition_expression: string;
  true_label?: string;
  false_label?: string;
}

export interface WhileLoopNodeConfig {
  condition_expression: string;
  max_iterations: number;
}

export interface UserApprovalNodeConfig {
  prompt_template: string;
  timeout_seconds?: number;
}

export interface TransformNodeConfig {
  transform_type: 'jinja2' | 'expression';
  template: string;
  output_key: string;
}

export interface SetStateNodeConfig {
  assignments: Array<{ key: string; value_expression: string }>;
}

export interface ToolNodeConfig {
  tool_key: string;
  tool_config?: Record<string, any>;
  input_mapping?: Record<string, string>;
  output_key?: string;
}

// Node palette definition for the toolbox
export interface NodePaletteItem {
  type: NodeType;
  label: string;
  description: string;
  category: 'flow' | 'agents' | 'logic' | 'data' | 'tools' | 'other';
  icon: string;
  color: string;
}

// New node config types for iteration and parallelism
export interface ForEachNodeConfig {
  collection_expression: string;
  item_variable: string;
  index_variable: string;
  output_key: string;
  parallel: boolean;
  concurrency_limit: number;
  continue_on_error: boolean;
  max_iterations: number;
}

export interface ParallelNodeConfig {
  branches: Array<{ label: string; target_node_key?: string }>;
  merge_strategy: 'dict' | 'list';
  output_key: string;
  continue_on_error: boolean;
  timeout_seconds: number;
}
