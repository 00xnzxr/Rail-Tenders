export interface CommandCenterSession {
  id: number;
  tender_id?: number;
  title?: string;
  status: string;
  mode: 'tender_linked' | 'standalone';
  agent_type: string;
  router_session_id?: string;
  pipeline_state: PipelineState;
  template_id?: string;
  current_version: number;
  created_at: string;
  updated_at?: string;
  // Extended fields from list endpoint
  tender_title?: string;
  tender_organisation?: string;
  tender_closing_date?: string;
  artifact_count?: number;
  message_count?: number;
  workspace_progress?: {
    total: number;
    completed: number;
    has_workspace: boolean;
  } | null;
}

export interface PipelineState {
  analyze_documents?: boolean;
  generate_checklist?: boolean;
  generate_documents?: boolean;
  research_costing?: boolean;
  workspace_setup?: boolean;
}

export interface Artifact {
  id: number;
  session_id: number;
  artifact_type: 'document' | 'cost_breakdown' | 'cost_breakdown_xlsx' | 'checklist' | 'analysis';
  title: string;
  content: string;
  structured_data?: any;
  version: number;
  parent_id?: number;
  agent_key?: string;
  message_id?: number;
  file_path?: string;
  file_name?: string;
  is_pinned: boolean;
  status: string;
  metadata_json?: any;
  created_by?: number;
  created_at: string;
  updated_at?: string;
}

export interface Suggestion {
  text: string;
  action: 'chat' | 'pipeline_step' | 'navigate';
  step?: string;
  icon: string;
  priority: number;
}

export interface RoutingInfo {
  intent: string;
  agents: string[];
  agentNames: string[];
  // One-sentence justification from the router's extended-thinking pass.
  // Surfaced to users so they can see WHY a particular agent was picked
  // ("routing to Costing Researcher because the user asked for rates").
  reasoning?: string;
}

export interface ChatMessage {
  id?: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  tool_calls?: any[];
  output_type?: string;
  routed_from?: string;
  metadata?: any;
  created_at?: string;
  attachments?: ChatAttachment[];
  // Streaming state
  isStreaming?: boolean;
  routingInfo?: RoutingInfo;
  activeAgent?: string;
  // Artifacts created during this message's generation
  artifact_refs?: ArtifactCreatedEvent[];
  // Decision Maker master-agent reasoning timeline
  timeline?: TimelineEvent[];
  timelineBudget?: TimelineBudget;
  // Decision Maker pending-plan approval card
  pendingPlan?: PendingPlan;
  planDecision?: 'approve' | 'change' | 'deny' | null;
  // Plan execution progress (post-approval) — captured from plan_step_progress events
  planProgress?: {
    title?: string | null;
    steps: Array<{
      index: number;
      total: number;
      agent?: string | null;
      agent_display_name?: string | null;
      description?: string;
      status: 'running' | 'completed' | 'failed' | 'skipped';
      error?: string | null;
    }>;
  } | null;
  // Typed reliability events from the backend — distinct from the streaming
  // content. The renderer shows these as separate bubbles (warning: amber,
  // error: red) so error / overflow messages never get rendered as agent
  // text inside the response. See plan: now-i-need-to-synchronous-taco.md.
  agent_notices?: AgentNotice[];
}

export interface AgentNotice {
  severity: 'warning' | 'error';
  /** Stable machine code, e.g. 'context_trimmed' | 'persistence_failed' | 'context_overflow' */
  code: string;
  message: string;
  details?: any;
  agent_key?: string;
}

export interface PendingPlanStep {
  description: string;
  agent?: string | null;
  rationale?: string | null;
}

export interface PendingPlan {
  title: string;
  reasoning: string;
  steps: PendingPlanStep[];
}

export interface TimelineEvent {
  step: number;
  type: 'thought' | 'action' | 'observation';
  // thought
  content?: string;
  // action
  tool?: string;
  input_preview?: string;
  // observation
  result_preview?: string;
  is_error?: boolean;
}

export interface TimelineBudget {
  iteration: number;
  max_iterations: number;
  elapsed_s: number;
  max_execution_time?: number;
}

export interface ArtifactCreatedEvent {
  artifact_id: number;
  artifact_type: string;
  title: string;
  version: number;
}

// Manpower & resource decomposition emitted by the costing agent —
// the agent's audit trail for how it derived its line items.
export interface ManpowerEntry {
  role?: string;
  headcount?: number;
  deployment?: string;
  rate_low_inr?: number | null;
  rate_high_inr?: number | null;
  rate_unit?: string;
}

export interface ResourceEntry {
  type?: string;
  item?: string;
  quantity_per_cycle?: string | number;
  frequency?: string;
  rate_low_inr?: number | null;
  rate_high_inr?: number | null;
  rate_unit?: string;
}

export interface ManpowerResourceBucket {
  scope_bucket?: string;
  manpower?: ManpowerEntry[];
  resources?: ResourceEntry[];
  volume_drivers?: string[];
}

// Phase 3b — flat rate-card library matching the reference Excel's Sheet 2.
// Grouped by `section` when rendered (4 standard sections: Materials,
// Major Component Repair, Electrical Spares, Labour & Overhead).
export interface CostAssumption {
  section?: string;
  item?: string;
  rate_inr?: number | null;
  uom?: string;
  source_ref?: string;
}

// Phase 3b — Sheet 1 strategic summary: tender snapshot + schedule
// profitability + actionable observations + recommended bid strategy.
export interface TenderSnapshot {
  tender_no?: string;
  tender_value_inr?: number | null;
  scope_one_liner?: string;
  period?: string;
  depots_or_locations?: string[];
  emd_inr?: number | null;
  performance_guarantee?: string;
  bid_validity_days?: number | null;
  penalty_cap_pct_of_contract?: number | null;
  min_eligibility?: string[];
}

export interface ScheduleBreakdownEntry {
  schedule?: string;
  tender_value_inr?: number | null;
  estimated_cost_inr?: number | null;
  gross_margin_inr?: number | null;
  gross_margin_pct?: number | null;
}

export interface StrategicSummary {
  tender_snapshot?: TenderSnapshot;
  schedule_breakdown?: ScheduleBreakdownEntry[];
  key_observations?: string[];
  recommended_bid_strategy?: string;
}

// Editable cost-breakdown row (one BOQ line item with rate provenance).
// `rate` / `amount` represent the expected band; range fields are populated
// when the costing agent supplied a low/high band. Phase 3b adds margin-
// analysis fields (tender_rate / margin_*) + schedule_section + cost_buildup_note.
export interface CostBreakdownLine {
  id?: number;
  sr_no: number;
  description: string;
  category?: string | null;
  quantity?: number | null;
  unit?: string | null;
  rate?: number | null;
  amount?: number | null;
  // Range fields
  rate_low?: number | null;
  rate_high?: number | null;
  amount_low?: number | null;
  amount_high?: number | null;
  // Per-line profit (varied by category — labour 14–18%, material 8–12%, etc.)
  profit_pct?: number | null;
  profit_amount?: number | null;
  profit_amount_low?: number | null;
  profit_amount_high?: number | null;
  // Phase 3b — margin-analysis fields (when the tender provides BOQ rates)
  tender_rate?: number | null;
  tender_amount?: number | null;
  margin_amount_low?: number | null;
  margin_amount?: number | null;
  margin_amount_high?: number | null;
  margin_pct?: number | null;
  schedule_section?: string | null;
  cost_buildup_note?: string | null;

  // NIT-mirror fields (RULE 5). Populated when the costing 1:1-mirrors the
  // tender's captured bidding schedule (BOQItem). The editor groups rows by
  // schedule_name, surfaces item_code / bidding_unit / escalation_pct as
  // read-only, and renders is_tax_line rows distinctly (no editable rate).
  boq_item_id?: number | null;
  item_code?: string | null;
  schedule_name?: string | null;
  bidding_unit?: string | null;
  basic_value?: number | null;
  escalation_pct?: number | null;
  is_tax_line?: boolean;
  annexure?: string | null;
  oem_manufacturer?: string | null;
  source_url?: string | null;

  // Annexure component link. A line with parent_boq_item_id set is one
  // material of the schedule item it points at: its quantity is per set and
  // its amount is already inside that item's rate, so it is shown under its
  // own group and left out of the totals.
  parent_boq_item_id?: number | null;
  annexure_ref?: string | null;

  rate_source?: string | null;
  source_ref?: string | null;
  confidence?: string | null;
  needs_input: boolean;
  notes?: string | null;
}

export interface CostBreakdown {
  id: number;
  tender_id: number;
  version: number;
  status: 'draft' | 'finalized';
  title?: string;
  overhead_percent: number;
  margin_percent: number;
  gst_percent: number;
  // Expected band (legacy single-value totals)
  subtotal: number;
  overhead_amount: number;
  margin_amount: number;
  gst_amount: number;
  grand_total: number;
  // Range bands — null when the breakdown was produced in single-rate mode
  subtotal_low?: number | null;
  subtotal_high?: number | null;
  overhead_amount_low?: number | null;
  overhead_amount_high?: number | null;
  margin_amount_low?: number | null;
  margin_amount_high?: number | null;
  gst_amount_low?: number | null;
  gst_amount_high?: number | null;
  grand_total_low?: number | null;
  grand_total_high?: number | null;

  // Phase 3b — margin-analysis roll-up (set when at least one line had tender_rate).
  tender_total?: number | null;
  margin_total_low?: number | null;
  margin_total?: number | null;
  margin_total_high?: number | null;
  margin_pct?: number | null;

  needs_input_count: number;
  assumptions: string[];
  recommendations: string[];
  manpower_resource_analysis?: ManpowerResourceBucket[];
  // Phase 3b — rate-card library + strategic summary (Sheet 1 + Sheet 2 of reference Excel)
  cost_assumptions?: CostAssumption[];
  strategic_summary?: StrategicSummary | null;
  created_by_agent?: string | null;
  session_id?: number | null;
  artifact_id?: number | null;
  created_at: string;
  updated_at?: string;
  lines: CostBreakdownLine[];
}

export interface ChatAttachment {
  id: number;
  file_name: string;
  file_type: string;
  file_size: number;
  // Local-only fields for preview before upload
  localFile?: File;
  previewUrl?: string;
}
