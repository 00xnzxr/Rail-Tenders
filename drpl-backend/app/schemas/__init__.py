"""
DRPL Backend - Pydantic Schemas
Request/response models for API validation
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


# --- Auth ---

class TokenRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str


# --- API Tokens ---

class CreateAPITokenRequest(BaseModel):
    name: str  # e.g., "Chrome Extension"

class CreateAPITokenResponse(BaseModel):
    id: int
    name: str
    token: str  # raw token, shown ONCE
    prefix: str
    created_at: datetime

class APITokenInfo(BaseModel):
    id: int
    name: str
    prefix: str
    last_used_at: Optional[datetime]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# --- User Profile ---

class UserProfileResponse(BaseModel):
    id: int
    email: str
    name: str
    role: str
    is_active: bool
    created_at: Optional[datetime]  # Optional to handle seeded users

    class Config:
        from_attributes = True


# --- Extension Status ---

class ExtensionStatusResponse(BaseModel):
    connected: bool
    last_sync: Optional[datetime]
    active_portals: list[str]
    tenders_uploaded_24h: int


# --- Tender Data (from Chrome Extension) ---

class TenderInput(BaseModel):
    portal: str
    tenderId: str
    title: str
    department: str = ""
    organisation: str = ""
    description: str = ""
    estimatedValue: Optional[float] = None
    currency: str = "INR"
    openingDate: Optional[str] = None
    closingDate: Optional[str] = None
    emdAmount: Optional[float] = None
    preBidDate: Optional[str] = None
    status: str = "open"
    documentLinks: list[str] = []
    sourceUrl: str = ""
    extractedAt: str = ""
    # Deep scrape fields
    detailUrl: Optional[str] = None
    fullDescription: Optional[str] = None
    eligibilityCriteria: Optional[str] = None
    technicalSpecifications: Optional[str] = None
    evaluationCriteria: Optional[str] = None
    performanceGuarantee: Optional[float] = None
    performanceGuaranteePercent: Optional[float] = None
    preBidMeetingLocation: Optional[str] = None
    deliveryLocation: Optional[str] = None
    deliveryTimeline: Optional[str] = None
    buyerContactName: Optional[str] = None
    buyerContactEmail: Optional[str] = None
    buyerContactPhone: Optional[str] = None
    numberOfAmendments: Optional[int] = None
    corrigendaLinks: list[str] = []
    nitDocumentLinks: list[str] = []
    amendmentLinks: list[str] = []
    isDetailExtracted: bool = False
    # Scope-driven scraping fields (Phase 7)
    searchMatchKeyword: Optional[str] = None         # Which scope-profile keyword surfaced this row (GeM auto-search)
    isEligibleIndicator: Optional[bool] = None       # IREPS blue-tick / arrow visual flag
    # Detailed-card fields (2026-07)
    location: Optional[str] = None
    bidType: Optional[str] = None
    sourcePortal: Optional[str] = None
    category: Optional[str] = None

class TenderBatchInput(BaseModel):
    tenders: list[TenderInput]

class BatchUploadResponse(BaseModel):
    received: int
    new: int
    duplicates: int
    errors: int
    new_ids: list[int] = []  # Phase 7 — IDs of newly inserted rows for async scoring fan-out

class TenderResponse(BaseModel):
    id: int
    portal: str
    tender_id: str
    title: str
    department: Optional[str] = None
    organisation: Optional[str] = None
    estimated_value: Optional[float]
    closing_date: Optional[datetime]
    status: str
    ai_relevance_score: Optional[float]
    priority: Optional[str] = "medium"
    workflow_status: Optional[str] = "new"
    assigned_to: Optional[int] = None
    eligibility_status: Optional[str] = None
    emd_amount: Optional[float] = None
    ai_summary: Optional[str] = None
    location: Optional[str] = None
    bid_type: Optional[str] = None
    source_portal: Optional[str] = None
    category: Optional[str] = None
    below_threshold: bool = False
    # Nullable column with no server_default — legacy rows can hold NULL, and
    # `?include_archived=true` is the one list path that does not filter them
    # out. A bare `bool` would 500 the list on those rows.
    is_archived: Optional[bool] = False
    archived_at: Optional[datetime] = None
    archive_reason: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TenderListResponse(BaseModel):
    items: list[TenderResponse]
    total: int


class TenderDetailResponse(BaseModel):
    id: int
    portal: str
    tender_id: str
    source_url: Optional[str]
    title: str
    department: Optional[str]
    organisation: Optional[str]
    description: Optional[str]
    estimated_value: Optional[float]
    currency: Optional[str]
    emd_amount: Optional[float]
    opening_date: Optional[datetime]
    closing_date: Optional[datetime]
    pre_bid_date: Optional[datetime]
    status: str
    document_links: Optional[list] = []
    ai_category: Optional[str]
    ai_relevance_score: Optional[float]
    ai_risk_score: Optional[float]
    ai_summary: Optional[str]
    priority: Optional[str] = "medium"
    workflow_status: Optional[str] = "new"
    assigned_to: Optional[int] = None
    assigned_at: Optional[datetime] = None
    submission_deadline: Optional[datetime] = None
    eligibility_status: Optional[str] = None
    eligibility_score: Optional[float] = None
    eligibility_notes: Optional[str] = None
    location: Optional[str] = None
    bid_type: Optional[str] = None
    source_portal: Optional[str] = None
    category: Optional[str] = None
    # Deep scrape fields
    detail_url: Optional[str] = None
    full_description: Optional[str] = None
    eligibility_criteria: Optional[str] = None
    technical_specifications: Optional[str] = None
    evaluation_criteria: Optional[str] = None
    performance_guarantee: Optional[float] = None
    performance_guarantee_percent: Optional[float] = None
    pre_bid_meeting_location: Optional[str] = None
    delivery_location: Optional[str] = None
    delivery_timeline: Optional[str] = None
    buyer_contact_name: Optional[str] = None
    buyer_contact_email: Optional[str] = None
    buyer_contact_phone: Optional[str] = None
    number_of_amendments: Optional[int] = None
    corrigenda_links: Optional[list] = []
    nit_document_links: Optional[list] = []
    amendment_links: Optional[list] = []
    is_detail_extracted: bool = False
    extracted_at: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class TenderUpdateInput(BaseModel):
    priority: Optional[str] = None
    workflow_status: Optional[str] = None
    submission_deadline: Optional[str] = None

class TenderAssignInput(BaseModel):
    user_id: int


# --- Checklist ---

class ChecklistItemResponse(BaseModel):
    id: int
    tender_id: int
    item_name: str
    item_description: Optional[str]
    is_required: bool
    is_uploaded: bool
    document_id: Optional[int]
    display_order: int
    created_at: datetime
    item_category: str = "standard"
    generation_status: str = "pending"
    generated_document_id: Optional[int] = None
    generation_error: Optional[str] = None
    ai_instructions: Optional[str] = None
    source_section: Optional[str] = None

    class Config:
        from_attributes = True

class ChecklistCompletionResponse(BaseModel):
    total: int
    uploaded: int
    remaining: int
    required: int
    required_uploaded: int
    complete: bool

class AddChecklistItemInput(BaseModel):
    item_name: str
    item_description: str = ""
    is_required: bool = True

class UpdateChecklistItemInput(BaseModel):
    item_name: Optional[str] = None
    item_description: Optional[str] = None
    is_required: Optional[bool] = None


# --- Proposals ---

class ProposalSessionResponse(BaseModel):
    id: int
    tender_id: Optional[int] = None
    created_by: int
    status: str
    title: Optional[str] = None
    agent_type: Optional[str] = "tender_proposal"
    template_id: Optional[str] = None
    current_version: int
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True

class ProposalMessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    message_type: str
    created_at: datetime

    class Config:
        from_attributes = True

class ProposalDocumentResponse(BaseModel):
    id: int
    session_id: int
    version: int
    file_name: str
    file_size: Optional[int]
    generated_at: datetime

    class Config:
        from_attributes = True

class ChatMessageInput(BaseModel):
    message: str

class CreateProposalSessionInput(BaseModel):
    tender_id: int

class CreateStandaloneSessionInput(BaseModel):
    title: str
    context: dict = {}

class ProposalReviewResponse(BaseModel):
    id: int
    session_id: int
    reviewer_id: int
    status: str
    comments: Optional[str]
    reviewed_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

class ReviewActionInput(BaseModel):
    comments: str = ""

class RAGDocumentResponse(BaseModel):
    filename: str
    size: int
    uploaded_at: Optional[str] = None


# --- Scrape Status ---

class ScrapeLogResponse(BaseModel):
    id: int
    user_id: int
    portal: str
    session_id: Optional[str]
    status: Optional[str]
    tenders_found: int
    error_message: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True

class ScrapeStatusInput(BaseModel):
    portal: str
    sessionId: str
    status: str
    tendersFound: int
    error: Optional[str] = None


# --- Extension Config ---

class ExtensionConfigResponse(BaseModel):
    selectors_version: str
    scrape_interval_minutes: int
    enabled_portals: list[str]
    features: dict
    scope_profile: Optional[dict] = None  # Phase 7 — driven by /admin/scope-profile


# --- Portal Health Monitoring ---

class PortalHealthInfo(BaseModel):
    portal: str
    status: str  # "healthy", "warning", "error"
    last_successful_scrape: Optional[datetime]
    tenders_24h: int
    tenders_7d: int
    tenders_30d: int
    success_rate: float  # 0.0 to 1.0
    last_error: Optional[str]
    selectors_version: str

class PortalAlert(BaseModel):
    portal: str
    alert_type: str  # "portal_dark", "selector_breakage", "error_spike"
    message: str
    severity: str  # "warning", "critical"
    detected_at: datetime


# --- AI Analysis ---

class AIAnalysisResponse(BaseModel):
    tender_id: int
    ai_category: Optional[str]
    ai_relevance_score: Optional[float]
    ai_risk_score: Optional[float]
    ai_summary: Optional[str]
    # Post-analysis fan-out summary (annexure_finder + checklist_generator +
    # workspace init). Present when /analyze is called with fanout=true
    # (default). Shape: {"annexure_finder": {...}, "checklist_generator": {...},
    # "workspace": {...}}.
    pipeline: Optional[dict] = None

class AIBatchResponse(BaseModel):
    analyzed: int
    remaining: int
    errors: int

class AIStatsResponse(BaseModel):
    total_analyzed: int
    total_unanalyzed: int
    avg_relevance: Optional[float]
    category_distribution: dict
    high_relevance_count: int  # score > 0.7


# --- Proposal Templates ---

class ProposalTemplateResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    template_type: str
    source: str
    structure_json: Optional[dict] = {}
    original_file_name: Optional[str]
    railway_zone: Optional[str]
    contract_type: Optional[str]
    bidding_system: Optional[str]
    is_active: bool
    output_format: Optional[str] = "docx"
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    class Config:
        from_attributes = True

class SetTemplateInput(BaseModel):
    template_id: int


# --- Workspace ---

class WorkspaceConfigResponse(BaseModel):
    id: int
    tender_id: int
    view_mode: str = "grid"
    layout_json: Optional[dict] = None
    default_letterhead_id: Optional[int] = None
    default_agent_key: Optional[str] = None
    total_items: int = 0
    completed_items: int = 0
    last_activity_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class DocumentWorkspaceResponse(BaseModel):
    id: int
    checklist_item_id: int
    tender_id: int
    agent_key: Optional[str] = None
    agent_config_override: Optional[dict] = None
    format_template_id: Optional[int] = None
    format_instructions: Optional[str] = None
    draft_content_html: Optional[str] = None
    draft_content_markdown: Optional[str] = None
    content_version: int = 0
    conversation_session_id: Optional[str] = None
    notes: Optional[str] = None
    review_status: str = "not_started"
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    letterhead_template_id: Optional[int] = None
    signatures_json: Optional[list] = []
    page_orientation: str = "portrait"
    depends_on: Optional[list] = []
    referenced_by: Optional[list] = []
    last_edited_at: Optional[datetime] = None
    last_edited_by: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class WorkspaceItemResponse(BaseModel):
    """Combined ChecklistItem + DocumentWorkspace data for the canvas view."""
    # From ChecklistItem
    id: int
    tender_id: int
    item_name: str
    item_description: Optional[str] = None
    is_required: bool = True
    is_uploaded: bool = False
    document_id: Optional[int] = None
    display_order: int = 0
    item_category: str = "standard"
    generation_status: str = "pending"
    generated_document_id: Optional[int] = None
    ai_instructions: Optional[str] = None
    source_section: Optional[str] = None
    is_not_required: bool = False
    workspace_status: str = "not_started"
    agent_key: Optional[str] = None
    # From DocumentWorkspace (joined)
    workspace_id: Optional[int] = None
    draft_content_html: Optional[str] = None
    content_version: int = 0
    review_status: str = "not_started"
    format_template_id: Optional[int] = None
    format_instructions: Optional[str] = None
    notes: Optional[str] = None
    last_edited_at: Optional[datetime] = None

class WorkspaceCategoryStats(BaseModel):
    total: int = 0
    completed: int = 0
    drafting: int = 0
    in_review: int = 0
    not_required: int = 0

class WorkspaceStatsResponse(BaseModel):
    total_items: int = 0
    completed_items: int = 0
    completion_percent: float = 0.0
    by_category: dict = {}   # {"standard": WorkspaceCategoryStats, ...}
    by_status: dict = {}     # {"not_started": N, "drafting": N, ...}

class WorkspaceOverviewResponse(BaseModel):
    config: WorkspaceConfigResponse
    items: list[WorkspaceItemResponse] = []
    stats: WorkspaceStatsResponse

class UpdateWorkspaceConfigInput(BaseModel):
    view_mode: Optional[str] = None
    default_letterhead_id: Optional[int] = None
    default_agent_key: Optional[str] = None

class UpdateWorkspaceLayoutInput(BaseModel):
    layout_json: dict

class UpdateDocumentWorkspaceInput(BaseModel):
    agent_key: Optional[str] = None
    format_template_id: Optional[int] = None
    format_instructions: Optional[str] = None
    notes: Optional[str] = None
    review_status: Optional[str] = None
    letterhead_template_id: Optional[int] = None
    signatures_json: Optional[list] = None
    depends_on: Optional[list] = None

class SaveDocumentContentInput(BaseModel):
    content_html: Optional[str] = None
    content_markdown: Optional[str] = None

class DocumentFormatTemplateResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    document_category: str
    structure_json: Optional[dict] = None
    content_template_markdown: Optional[str] = None
    content_template_html: Optional[str] = None
    format_rules: Optional[list] = []
    required_sections: Optional[list] = []
    match_patterns: Optional[list] = []
    is_system: bool = False
    is_active: bool = True
    output_format: Optional[str] = "docx"
    original_file_name: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CreateFormatTemplateInput(BaseModel):
    name: str
    description: Optional[str] = None
    document_category: str
    structure_json: Optional[dict] = None
    content_template_markdown: Optional[str] = None
    content_template_html: Optional[str] = None
    format_rules: Optional[list] = []
    required_sections: Optional[list] = []
    match_patterns: Optional[list] = []


class UpdateFormatTemplateInput(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    document_category: Optional[str] = None
    structure_json: Optional[dict] = None
    content_template_markdown: Optional[str] = None
    content_template_html: Optional[str] = None
    format_rules: Optional[list] = None
    required_sections: Optional[list] = None
    match_patterns: Optional[list] = None
    is_active: Optional[bool] = None


class DependencyItemResponse(BaseModel):
    id: int
    item_name: str
    item_category: str
    review_status: str
    has_content: bool
    content_version: int

    class Config:
        from_attributes = True


class DocumentDependenciesResponse(BaseModel):
    depends_on: list[DependencyItemResponse] = []
    referenced_by: list[DependencyItemResponse] = []


# ── Unified Template Management ─────────────────────────────────────────────

class UnifiedTemplateResponse(BaseModel):
    """Normalized response for any template type (proposal, document, costing)."""
    id: int
    template_kind: str                          # "proposal", "document", "costing"
    name: str
    description: Optional[str] = None
    category: Optional[str] = None              # template_type / document_category / format_type
    source: Optional[str] = None                # system, uploaded, reverse_engineered
    is_system: bool = False
    is_active: bool = True
    output_format: str = "docx"                 # docx, xlsx, pdf
    original_file_name: Optional[str] = None
    structure_summary: Optional[dict] = None    # {sections: N, fields: N, columns: N}
    metadata: Optional[dict] = None             # kind-specific metadata
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CostingTemplateResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    zone: str
    format_type: str
    column_definitions: Optional[list] = []
    footer_rows: Optional[list] = []
    html_template: Optional[str] = None
    markdown_template: Optional[str] = None
    sample_pdf_path: Optional[str] = None
    output_format: Optional[str] = "xlsx"
    original_file_name: Optional[str] = None
    is_default: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CreateCostingTemplateInput(BaseModel):
    name: str
    zone: str
    format_type: str = "boq"
    description: Optional[str] = None
    column_definitions: Optional[list] = []
    footer_rows: Optional[list] = []
    html_template: Optional[str] = None
    markdown_template: Optional[str] = None


class UpdateCostingTemplateInput(BaseModel):
    name: Optional[str] = None
    zone: Optional[str] = None
    format_type: Optional[str] = None
    description: Optional[str] = None
    column_definitions: Optional[list] = None
    footer_rows: Optional[list] = None
    html_template: Optional[str] = None
    markdown_template: Optional[str] = None
    is_default: Optional[bool] = None
