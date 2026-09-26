// ============================================================
// DRPL Extension - Shared Types
// ============================================================

// --- Portal Types ---

export type PortalName = 'ireps' | 'gem' | 'tendertiger' | 'bidassist' | 'tenderdetail' | 'tendersinfo' | 'projectstoday';

export type PageType =
  | 'tender_listing'
  | 'tender_detail'
  | 'search_results'
  | 'bid_status'
  | 'seller_dashboard'
  | 'unknown';

// --- Document Link Classification ---

export interface DocumentLinkInfo {
  url: string;
  label: string;
  type: 'nit' | 'corrigendum' | 'amendment' | 'specification' | 'other';
}

// --- Extracted Tender Data ---

export interface TenderData {
  portal: PortalName;
  tenderId: string;           // Unique tender number from the portal
  title: string;
  department: string;         // e.g., "Mechanical", "Electrical"
  organisation: string;       // e.g., railway zone, buyer org
  description: string;
  estimatedValue: number | null;
  currency: string;           // "INR"
  openingDate: string | null; // ISO date
  closingDate: string | null; // ISO date
  emdAmount: number | null;
  preBidDate: string | null;
  status: string;             // e.g., "Open", "Closed", "Awarded"
  documentLinks: string[];    // URLs to NIT PDFs, corrigenda, etc.
  sourceUrl: string;          // The page URL this was extracted from
  extractedAt: string;        // ISO timestamp of extraction
  rawHtml?: string;           // Optional: raw HTML snippet for debugging
  eligibilityStatus?: 'eligible' | 'not_eligible' | 'unknown';
  eligibilityScore?: number;
  eligibilityNotes?: string;

  // --- Deep scrape fields (populated from detail pages) ---
  detailUrl?: string;                    // Link to tender detail page
  fullDescription?: string;              // Full scope/description from detail page
  eligibilityCriteria?: string;          // Experience, turnover, certifications text
  technicalSpecifications?: string;      // Tech specs from detail page
  evaluationCriteria?: string;           // How bids are evaluated
  performanceGuarantee?: number | null;
  performanceGuaranteePercent?: number | null;
  preBidMeetingLocation?: string;
  deliveryLocation?: string;
  deliveryTimeline?: string;
  buyerContactName?: string;
  buyerContactEmail?: string;
  buyerContactPhone?: string;
  numberOfAmendments?: number;
  corrigendaLinks?: string[];            // Corrigendum-specific PDFs
  nitDocumentLinks?: string[];           // NIT/tender notice PDFs
  amendmentLinks?: string[];             // Amendment PDFs
  classifiedDocuments?: DocumentLinkInfo[]; // All docs with classification
  isDetailExtracted?: boolean;           // Flag: detail page was visited

  // --- Phase 7: scope-driven scraping ---
  searchMatchKeyword?: string;           // GeM auto-search: which scope keyword surfaced this row
  isEligibleIndicator?: boolean;         // IREPS: blue tick / arrow visual indicator (buyer-flagged eligible)

  // --- Detailed-card fields (2026-07) ---
  location?: string;                     // "Saran, Bihar, India"
  bidType?: string;                      // NCB / GCB / Limited / Single
  sourcePortal?: string;                 // originating portal for aggregator listings (e.g. "gem")
  category?: string;                     // portal-stated sector/category
}

// --- Phase 7: Scope Profile (synced from /api/extension/config) ---

export interface ScopeKeywordGroup {
  label: string;
  keywords: string[];
}

export interface HistoricalFallback {
  keywords: string[];
  ministries: string[];
  departments: string[];
}

export interface ScopeProfile {
  name: string;
  keyword_groups: ScopeKeywordGroup[];
  exclusion_terms: string[];
  target_ministries: string[];
  value_min: number | null;
  value_max: number | null;
  relevance_threshold: number;
  /** Per-keyword pagination cap for the auto-search driver. Default 5. */
  max_pages_per_keyword?: number;
  /** Fallback search hints derived server-side from past tenders. Used when
   * the scope profile is sparse. */
  historical_fallback?: HistoricalFallback;
  is_active: boolean;
  updated_at: string | null;
}

export interface KeywordRunStat {
  keyword: string;
  hits: number;
  pages: number;
  errors: number;
  lastRunAt: string;
}

// --- Message Passing ---

export type MessageType =
  | 'TENDER_DATA_EXTRACTED'
  | 'START_GUIDED_SCRAPE'
  | 'SCRAPE_STATUS_UPDATE'
  | 'SESSION_EXPIRED'
  | 'SELECTOR_UPDATE_AVAILABLE'
  | 'GET_SCRAPE_STATUS'
  | 'GET_CONFIG'
  | 'PAGE_NAVIGATION'
  | 'SCRAPE_PAGE_COMPLETE'
  | 'SCRAPE_CURRENT_TAB'
  | 'EXTRACT_DETAIL'
  | 'DETAIL_PAGE_EXTRACTED'
  | 'START_DEEP_SCRAPE'
  | 'DEEP_SCRAPE_PROGRESS'
  | 'TRIGGER_SCRAPE'
  // Phase 7 — scope-driven scraping
  | 'START_GEM_AUTO_SEARCH'        // popup -> service worker -> GeM advance-search tab
  | 'RUN_GEM_AUTO_SEARCH'          // service worker -> gem-search-driver content script
  | 'GEM_AUTO_SEARCH_PROGRESS'     // driver -> service worker (per-keyword stats)
  | 'GEM_AUTO_SEARCH_COMPLETE'
  | 'START_IREPS_ELIGIBLE_SCAN'    // popup -> service worker -> IREPS tab
  | 'START_IREPS_AUTO_SEARCH'      // popup -> service worker -> IREPS advance-search tab
  | 'RUN_IREPS_AUTO_SEARCH'        // service worker -> IREPS content script
  | 'IREPS_AUTO_SEARCH_SUBMITTED'  // content script -> service worker (search submitted)
  | 'IREPS_SEARCH_PAGINATION'      // content script -> service worker (walk result pages)
  | 'START_DOCUMENT_HUNT'          // popup -> service worker (deep document hunt)
  | 'HUNT_DOCS_FOR_TENDER'         // external -> service worker (single-tender on-demand doc fetch)
  | 'SYNC_SCOPE_PROFILE';          // popup -> service worker (refetch /config)

export interface ExtensionMessage {
  type: MessageType;
  payload?: any;
  portal?: PortalName;
}

export interface TenderDataMessage extends ExtensionMessage {
  type: 'TENDER_DATA_EXTRACTED';
  payload: {
    tenders: TenderData[];
    pageType: PageType;
    pageUrl: string;
  };
}

export interface ScrapeStatusMessage extends ExtensionMessage {
  type: 'SCRAPE_STATUS_UPDATE';
  payload: {
    portal: PortalName;
    status: 'running' | 'completed' | 'error' | 'session_expired';
    tendersFound: number;
    currentPage: number;
    totalPages: number | null;
    error?: string;
  };
}

// --- Deep Scrape Types ---

export interface DeepScrapeProgress {
  portal: PortalName;
  phase: 'extracting_details' | 'downloading_docs' | 'uploading' | 'completed' | 'error';
  current: number;
  total: number;
  currentTenderId: string;
  documentsDownloaded?: number;
  error?: string;
}

// --- Quality-first per-tender progress ---

export interface TenderProgress {
  tenderId: string;
  title: string;
  status: 'pending' | 'extracting' | 'downloading' | 'uploading' | 'completed' | 'error';
  documentsTotal: number;
  documentsCompleted: number;
  documentsUploaded: number;
  startedAt: string | null;
  completedAt: string | null;
  error?: string;
}

export interface ScrapeJobProgress {
  jobId: string;
  startedAt: string;
  totalTenders: number;
  currentTenderIndex: number;
  tenders: TenderProgress[];
  averageMsPerTender: number;
  status: 'running' | 'completed' | 'error' | 'cancelled';
  totalDocsUploaded: number;
  errorsCount: number;
}

// --- Storage Schema ---

export interface StorageData {
  authToken: string | null;           // JWT for DRPL backend
  backendUrl: string;                 // Base URL of DRPL backend API
  userId: string | null;
  userEmail: string | null;           // User's email for display
  extractedTenderIds: string[];       // For local deduplication
  lastSyncTimestamp: string | null;
  settings: ExtensionSettings;
  scrapeHistory: ScrapeSession[];
}

export interface ExtensionSettings {
  passiveMode: boolean;               // Auto-extract on page visit
  notificationsEnabled: boolean;
  scrapeIntervalMinutes: number;      // For scheduled mode
  maxTendersPerBatch: number;
  enabledPortals: PortalName[];
  deepScrapeEnabled: boolean;         // Auto-deep-scrape after listing scrape
  deepScrapeDelayMs: number;          // Rate limit delay (default 2500ms)
  downloadDocumentsEnabled: boolean;  // Auto-download PDFs from detail pages
}

export interface ScrapeSession {
  id: string;
  portal: PortalName;
  startedAt: string;
  completedAt: string | null;
  tendersFound: number;
  status: 'running' | 'uploading' | 'deep_scraping' | 'completed' | 'error';
  uploadedCount?: number;
  error?: string;
  deepScrapeTotal?: number;
  deepScrapeCompleted?: number;
  documentsDownloaded?: number;
}

// --- API Types ---

export interface ApiResponse<T = any> {
  success: boolean;
  data?: T;
  error?: string;
}

export interface BatchUploadResponse {
  received: number;
  new: number;
  duplicates: number;
  errors: number;
}
