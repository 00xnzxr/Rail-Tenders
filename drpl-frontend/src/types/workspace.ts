export interface WorkspaceConfig {
  id: number;
  tender_id: number;
  view_mode: 'grid' | 'kanban' | 'list' | 'canvas';
  layout_json: CanvasLayout | null;
  default_letterhead_id: number | null;
  default_agent_key: string | null;
  total_items: number;
  completed_items: number;
  last_activity_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WorkspaceItem {
  // From ChecklistItem
  id: number;
  tender_id: number;
  item_name: string;
  item_description: string | null;
  is_required: boolean;
  is_uploaded: boolean;
  document_id: number | null;
  display_order: number;
  item_category: 'standard' | 'generated' | 'analysis';
  generation_status: string;
  generated_document_id: number | null;
  ai_instructions: string | null;
  source_section: string | null;
  is_not_required: boolean;
  workspace_status: string;
  agent_key: string | null;
  // From DocumentWorkspace (joined)
  workspace_id: number | null;
  draft_content_html: string | null;
  content_version: number;
  review_status: 'not_started' | 'drafting' | 'in_review' | 'approved' | 'rejected';
  format_template_id: number | null;
  format_instructions: string | null;
  notes: string | null;
  last_edited_at: string | null;
}

export interface WorkspaceCategoryStats {
  total: number;
  completed: number;
  drafting: number;
  in_review: number;
  not_required: number;
}

export interface WorkspaceStats {
  total_items: number;
  completed_items: number;
  completion_percent: number;
  by_category: Record<string, WorkspaceCategoryStats>;
  by_status: Record<string, number>;
}

export interface WorkspaceOverview {
  config: WorkspaceConfig;
  items: WorkspaceItem[];
  stats: WorkspaceStats;
}

export interface DocumentWorkspaceDetail {
  item: {
    id: number;
    item_name: string;
    item_description: string | null;
    item_category: string;
    is_required: boolean;
    is_not_required: boolean;
    generation_status: string;
    ai_instructions: string | null;
    source_section: string | null;
    generated_document_id: number | null;
  };
  workspace: {
    id: number;
    checklist_item_id: number;
    tender_id: number;
    agent_key: string | null;
    agent_config_override: Record<string, unknown> | null;
    format_template_id: number | null;
    format_instructions: string | null;
    draft_content_html: string | null;
    draft_content_markdown: string | null;
    content_version: number;
    conversation_session_id: string | null;
    notes: string | null;
    review_status: string;
    reviewed_by: number | null;
    reviewed_at: string | null;
    letterhead_template_id: number | null;
    /** Explicit "no letterhead" opt-out. When false and letterhead_template_id
     *  is null, the tender-wide default applies. */
    letterhead_disabled?: boolean;
    signatures_json: Array<{
      signature_id: number;
      position: string;
      position_x?: number;
      position_y?: number;
      page: string | number;
    }>;
    page_orientation: 'portrait' | 'landscape';
    depends_on: number[];
    referenced_by: number[];
    last_edited_at: string | null;
    last_edited_by: number | null;
  };
  format_template: DocumentFormatTemplate | null;
}

export interface DocumentFormatTemplate {
  id: number;
  name: string;
  description: string | null;
  document_category: string;
  structure_json: Record<string, unknown> | null;
  content_template_markdown: string | null;
  content_template_html: string | null;
  format_rules: string[];
  required_sections: string[];
  match_patterns: string[];
  is_system: boolean;
  is_active: boolean;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface DependencyItem {
  id: number;
  item_name: string;
  item_category: string;
  review_status: string;
  has_content: boolean;
  content_version: number;
}

export interface DocumentDependencies {
  depends_on: DependencyItem[];
  referenced_by: DependencyItem[];
}

export interface CanvasNodePosition {
  x: number;
  y: number;
}

export interface CanvasViewport {
  x: number;
  y: number;
  zoom: number;
}

export interface CanvasLayout {
  canvas?: {
    positions: Record<string, CanvasNodePosition>;
    viewport?: CanvasViewport;
  };
  [key: string]: unknown;
}
