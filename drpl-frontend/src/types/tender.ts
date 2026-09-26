export interface Tender {
  id: number;
  portal: string;
  tender_id: string;
  title: string;
  department: string;
  organisation: string;
  estimated_value: number | null;
  closing_date: string | null;
  status: string;
  ai_relevance_score: number | null;
  priority: string;
  workflow_status: string;
  assigned_to: number | null;
  eligibility_status: string | null;
  created_at: string;
  location: string | null;
  bid_type: string | null;
  source_portal: string | null;
  category: string | null;
  emd_amount: number | null;
  ai_summary: string | null;
  below_threshold: boolean;
}

/** Reason a tender landed in the archive. Only `past_due` rows are ever auto-purged. */
export type ArchiveReason = 'past_due' | 'auto_discard' | 'manual';

/** A row from `GET /api/tenders/archive`. */
export interface ArchivedTender {
  id: number;
  portal: string;
  tender_id: string;
  title: string;
  department: string | null;
  estimated_value: number | null;
  closing_date: string | null;
  ai_relevance_score: number | null;
  archived_at: string | null;
  archive_reason: ArchiveReason | null;
  /**
   * When this row will be hard-deleted by the purge sweep.
   * NULL unless `archive_reason === 'past_due'` — the backend deliberately
   * omits it for auto_discard/manual rows because those are never auto-purged.
   * Never render a countdown when this is null.
   */
  purge_at: string | null;
}

export interface TenderDetail {
  id: number;
  portal: string;
  tender_id: string;
  source_url: string | null;
  title: string;
  department: string | null;
  organisation: string | null;
  description: string | null;
  estimated_value: number | null;
  currency: string | null;
  emd_amount: number | null;
  opening_date: string | null;
  closing_date: string | null;
  pre_bid_date: string | null;
  status: string;
  document_links: string[];
  ai_category: string | null;
  ai_relevance_score: number | null;
  ai_risk_score: number | null;
  ai_summary: string | null;
  priority: string;
  workflow_status: string;
  assigned_to: number | null;
  assigned_at: string | null;
  submission_deadline: string | null;
  extracted_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface TenderStats {
  total_tenders: number;
  by_portal: Record<string, number>;
  open_tenders: number;
  analyzed_tenders: number;
  avg_relevance: number | null;
  promising_count?: number;
  promising_open_count?: number;
  closing_soon_count?: number;
  to_bid_count?: number;
  with_costing_count?: number;
  scored_tenders?: number;
  unscored_tenders?: number;
  /** Where every row in the table actually is. Buckets are mutually exclusive
   *  and sum to all_rows, so a headline number dropping never reads as loss. */
  bifurcation?: {
    live: number;
    expired: number;
    archived: number;
    below_threshold: number;
    all_rows: number;
  };
}

export interface TenderFilters {
  portal?: string;
  /** Case-insensitive, multi-keyword title search. */
  search?: string;
  department?: string;
  status?: string;
  priority?: string;
  workflow_status?: string;
  assigned_to?: number;
  sort_by?: string;
  bid_type?: string;
  location?: string;
  segment?: string;
  score_min?: number;
  score_max?: number;
  value_min?: number;
  value_max?: number;
  emd_min?: number;
  emd_max?: number;
  closing_after?: string;
  closing_before?: string;
  eligibility_status?: string;
  unscored?: boolean;
  include_below_threshold?: boolean;
  /** Hide tenders whose closing date has passed. Tenders with no closing date
   *  are kept — an unknown date is not an expired one. View-only: nothing is
   *  archived or deleted. */
  exclude_expired?: boolean;
  limit: number;
  offset: number;
}

export interface AIBatchResponse {
  analyzed: number;
  remaining: number;
  errors: number;
}

export interface AIStatsResponse {
  total_analyzed: number;
  total_unanalyzed: number;
  avg_relevance: number | null;
  category_distribution: Record<string, number>;
  high_relevance_count: number;
}

// --- Checklist ---
export type ChecklistItemCategory = 'standard' | 'generated' | 'analysis';
export type ChecklistGenerationStatus = 'pending' | 'queued' | 'generating' | 'generated' | 'review' | 'approved' | 'failed';

export interface ChecklistItem {
  id: number;
  tender_id: number;
  item_name: string;
  item_description: string | null;
  is_required: boolean;
  is_uploaded: boolean;
  document_id: number | null;
  display_order: number;
  created_at: string;
  item_category: ChecklistItemCategory;
  generation_status: ChecklistGenerationStatus;
  generated_document_id: number | null;
  generation_error: string | null;
  ai_instructions: string | null;
  source_section: string | null;
}

export interface ChecklistCompletion {
  total: number;
  uploaded: number;
  remaining: number;
  required: number;
  required_uploaded: number;
  complete: boolean;
}

// --- Proposals ---
export interface ProposalSession {
  id: number;
  tender_id: number | null;
  created_by: number;
  status: string;
  title: string | null;
  agent_type: string;
  template_id: string | null;
  current_version: number;
  created_at: string;
  updated_at: string | null;
}

export interface ProposalMessage {
  id: number;
  session_id: number;
  role: string;
  content: string;
  message_type: string;
  created_at: string;
}

export interface ProposalReview {
  id: number;
  session_id: number;
  reviewer_id: number;
  status: string;
  comments: string | null;
  reviewed_at: string | null;
  created_at: string;
}

export interface PendingReview {
  id: number;
  session_id: number;
  tender_id: number | null;
  tender_title: string;
  tender_portal: string;
  status: string;
  created_at: string | null;
}
