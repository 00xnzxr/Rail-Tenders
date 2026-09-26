import axios from 'axios';
import type { AuthToken, LoginCredentials, UserProfile, CreateAPITokenResponse, APITokenInfo, ExtensionStatus } from '../types/auth';
import type { Tender, TenderDetail, TenderStats, TenderFilters, AIBatchResponse, AIStatsResponse, ChecklistItem, ChecklistCompletion, PendingReview, ArchivedTender } from '../types/tender';
import type { ScrapeSession } from '../types/scrape';
import type { PortalHealthInfo, PortalAlert } from '../types/monitoring';

// Where the API lives, relative to this bundle.
//
// Three cases, and the middle one is the reason this is not a plain `||`:
//   * VITE_API_URL set to a URL  -> a backend on another origin (CORS applies);
//   * VITE_API_URL set to ""     -> same origin. The production deployment
//     serves the SPA and the API from one domain via a rewrite, so every
//     `${API_BASE}/api/...` has to resolve to a relative path. `|| ...` folded
//     that empty string into the dev default and pointed the deployed site at
//     the visitor's own localhost;
//   * unset -> localhost in `vite dev`, same origin in a production build.
const _apiUrl = import.meta.env.VITE_API_URL as string | undefined;
export const API_BASE = (
  _apiUrl !== undefined ? _apiUrl : import.meta.env.DEV ? 'http://localhost:8000' : ''
).replace(/\/+$/, '');

export const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
});

// Attach JWT to every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('drpl_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle 401 — clear token and redirect
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('drpl_token');
      localStorage.removeItem('drpl_email');
      localStorage.removeItem('drpl_user_id');
      window.location.href = '/login';
    }
    // 402 = the monthly AI budget is exhausted. Repaint the usage bar so the
    // user sees *why* the run refused, instead of a bare error toast next to a
    // bar still showing the value it had before this request.
    if (error.response?.status === 402) {
      window.dispatchEvent(new Event('drpl:usage-refresh'));
    }
    return Promise.reject(error);
  }
);

// --- Auth ---
export async function login(credentials: LoginCredentials): Promise<AuthToken> {
  const { data } = await api.post<AuthToken>('/api/auth/token', credentials);
  return data;
}

export async function getCurrentUser(): Promise<UserProfile> {
  const { data } = await api.get<UserProfile>('/api/auth/me');
  return data;
}

// --- API Tokens ---
export async function createAPIToken(name: string): Promise<CreateAPITokenResponse> {
  const { data } = await api.post<CreateAPITokenResponse>('/api/auth/api-tokens', { name });
  return data;
}

export async function getAPITokens(): Promise<APITokenInfo[]> {
  const { data } = await api.get<APITokenInfo[]>('/api/auth/api-tokens');
  return data;
}

export async function revokeAPIToken(tokenId: number): Promise<void> {
  await api.delete(`/api/auth/api-tokens/${tokenId}`);
}

// --- Extension Status ---
export async function getExtensionStatus(): Promise<ExtensionStatus> {
  const { data } = await api.get<ExtensionStatus>('/api/extension/status/live');
  return data;
}

// --- Tenders ---
export async function getTenders(filters: TenderFilters): Promise<{ items: Tender[]; total: number }> {
  const params: Record<string, string | number> = { limit: filters.limit, offset: filters.offset };
  if (filters.portal) params.portal = filters.portal;
  if (filters.search) params.search = filters.search;
  if (filters.department) params.department = filters.department;
  if (filters.status) params.status = filters.status;
  if (filters.priority) params.priority = filters.priority;
  if (filters.workflow_status) params.workflow_status = filters.workflow_status;
  if (filters.assigned_to) params.assigned_to = filters.assigned_to;
  if (filters.sort_by) params.sort_by = filters.sort_by;
  if (filters.bid_type) params.bid_type = filters.bid_type;
  if (filters.location) params.location = filters.location;
  if (filters.segment) params.segment = filters.segment;
  if (filters.score_min != null) params.score_min = filters.score_min;
  if (filters.score_max != null) params.score_max = filters.score_max;
  if (filters.value_min != null) params.value_min = filters.value_min;
  if (filters.value_max != null) params.value_max = filters.value_max;
  if (filters.emd_min != null) params.emd_min = filters.emd_min;
  if (filters.emd_max != null) params.emd_max = filters.emd_max;
  if (filters.closing_after) params.closing_after = filters.closing_after;
  if (filters.closing_before) params.closing_before = filters.closing_before;
  if (filters.eligibility_status) params.eligibility_status = filters.eligibility_status;
  if (filters.unscored) params.unscored = 'true';
  if (filters.include_below_threshold) params.include_below_threshold = 'true';
  // Sent explicitly in BOTH directions: the route defaults to hiding expired,
  // so omitting this when the user asked to see them would silently hide them.
  if (filters.exclude_expired != null) params.exclude_expired = String(filters.exclude_expired);

  const { data } = await api.get<{ items: Tender[]; total: number }>('/api/tenders/', { params });
  return data;
}

export interface TenderViewCounts {
  to_bid: number;
  worth_a_look: number;
  discarded: number;
  new_unscored: number;
  all: number;
}

/** Funnel pile counts. Pass the same excludeExpired the list is using so the
 *  piles always agree with the rows on screen. */
export async function getTenderViewCounts(excludeExpired = true): Promise<TenderViewCounts> {
  const { data } = await api.get('/api/tenders/view-counts', {
    params: { exclude_expired: String(excludeExpired) },
  });
  return data as TenderViewCounts;
}

// --- Archive ---
export async function getArchivedTenders(
  params: { limit?: number; offset?: number; reason?: string } = {},
): Promise<{ items: ArchivedTender[]; total: number }> {
  const { data } = await api.get<{ items: ArchivedTender[]; total: number }>('/api/tenders/archive', { params });
  return data;
}

export async function restoreTender(id: number): Promise<{ id: number; restored: boolean }> {
  const { data } = await api.post(`/api/tenders/${id}/restore`);
  return data;
}

/**
 * Hard-delete archived tenders. Max 50 ids per call (server-enforced).
 * `skipped` counts DISTINCT ids the server refused because they were not
 * archived (already purged, restored, or unknown) — `deleted + skipped`
 * equals the distinct id count, not necessarily `ids.length`.
 */
export async function purgeTenders(ids: number[]): Promise<{ deleted: number; skipped: number }> {
  const { data } = await api.post('/api/tenders/archive/purge-now', { tender_ids: ids });
  return data;
}

export async function getTenderById(id: number): Promise<TenderDetail> {
  const { data } = await api.get<TenderDetail>(`/api/tenders/${id}`);
  return data;
}

/** Manually move a tender between piles (to_bid / not_bidable / discarded). */
export async function overrideTenderSegment(
  id: number,
  segment: 'to_bid' | 'not_bidable' | 'discarded',
): Promise<{ id: number; segment: string; segment_overridden: boolean }> {
  const { data } = await api.post(`/api/tenders/${id}/segment`, { segment });
  return data;
}

export interface TenderDocumentInfo {
  id: number;
  file_name: string;
  file_size: number;
  mime_type: string;
  document_type: string;
  extraction_status: string;
  source_url: string | null;
  uploaded_at: string | null;
}

export async function getTenderDocuments(tenderId: number): Promise<{ documents: TenderDocumentInfo[]; total: number }> {
  const { data } = await api.get(`/api/tenders/${tenderId}/documents`);
  return data;
}

export function getTenderDocumentViewUrl(tenderId: number, documentId: number): string {
  return `${API_BASE}/api/tenders/${tenderId}/documents/${documentId}/view`;
}

export async function updateTender(id: number, updates: { priority?: string; workflow_status?: string; submission_deadline?: string }): Promise<TenderDetail> {
  const { data } = await api.patch<TenderDetail>(`/api/tenders/${id}`, updates);
  return data;
}

export async function assignTender(id: number, userId: number): Promise<TenderDetail> {
  const { data } = await api.post<TenderDetail>(`/api/tenders/${id}/assign`, { user_id: userId });
  return data;
}

export async function getUsers(): Promise<UserProfile[]> {
  const { data } = await api.get<UserProfile[]>('/api/tenders/users');
  return data;
}

export async function getTenderStats(): Promise<TenderStats> {
  const { data } = await api.get<TenderStats>('/api/tenders/stats');
  return data;
}

// --- AI Analysis ---
export async function analyzeTender(tenderId: number): Promise<void> {
  await api.post(`/api/tenders/${tenderId}/analyze`);
}

export async function analyzeBatch(batchSize: number = 10, useBatchApi: boolean = false): Promise<any> {
  const { data } = await api.post(`/api/tenders/analyze-batch?batch_size=${batchSize}&use_batch_api=${useBatchApi}`);
  return data;
}

// --- Batch Processing (Claude Message Batches API) ---

export async function createBatchAnalysis(batchSize: number = 50, agents?: string): Promise<any> {
  const params = new URLSearchParams({ batch_size: batchSize.toString() });
  if (agents) params.append('agents', agents);
  const { data } = await api.post(`/api/batch/tender-analysis?${params}`);
  return data;
}

export async function listBatches(status?: string, limit: number = 20): Promise<any[]> {
  const params = new URLSearchParams({ limit: limit.toString() });
  if (status) params.append('status', status);
  const { data } = await api.get(`/api/batch/?${params}`);
  return data;
}

export async function getBatchDetail(batchId: string): Promise<any> {
  const { data } = await api.get(`/api/batch/${batchId}`);
  return data;
}

export async function getBatchItems(batchId: string, status?: string): Promise<any[]> {
  const params = new URLSearchParams();
  if (status) params.append('status', status);
  const { data } = await api.get(`/api/batch/${batchId}/items?${params}`);
  return data;
}

export async function pollBatch(batchId: string, autoProcess: boolean = true): Promise<any> {
  const { data } = await api.post(`/api/batch/${batchId}/poll?auto_process=${autoProcess}`);
  return data;
}

export async function processBatchResults(batchId: string): Promise<any> {
  const { data } = await api.post(`/api/batch/${batchId}/process`);
  return data;
}

export async function cancelBatch(batchId: string): Promise<any> {
  const { data } = await api.post(`/api/batch/${batchId}/cancel`);
  return data;
}

export async function getBatchStats(): Promise<any> {
  const { data } = await api.get('/api/batch/stats');
  return data;
}

// --- Context Management ---

export async function getContextModels(): Promise<any[]> {
  const { data } = await api.get('/api/context/models');
  return data;
}

export async function getContextConfig(model?: string): Promise<any> {
  const params = model ? `?model=${model}` : '';
  const { data } = await api.get(`/api/context/config${params}`);
  return data;
}

export async function countTokens(body: any): Promise<any> {
  const { data } = await api.post('/api/context/count-tokens', body);
  return data;
}

export async function getCacheStats(days: number = 30): Promise<any> {
  const { data } = await api.get(`/api/context/cache-stats?days=${days}`);
  return data;
}

export async function getAIStats(): Promise<AIStatsResponse> {
  const { data } = await api.get<AIStatsResponse>('/api/tenders/ai-stats');
  return data;
}

// --- Scrape Logs ---
export async function getScrapeLogs(
  portal?: string,
  limit: number = 50,
  offset: number = 0
): Promise<ScrapeSession[]> {
  const params: Record<string, string | number> = { limit, offset };
  if (portal) params.portal = portal;
  const { data } = await api.get<ScrapeSession[]>('/api/tenders/scrape-logs/', { params });
  return data;
}

// --- Extension Config ---
export async function getExtensionConfig() {
  const { data } = await api.get('/api/extension/config');
  return data;
}

// --- Monitoring ---
export async function getPortalHealth(): Promise<PortalHealthInfo[]> {
  const { data } = await api.get<PortalHealthInfo[]>('/api/monitoring/portal-health');
  return data;
}

export async function getPortalAlerts(): Promise<PortalAlert[]> {
  const { data } = await api.get<PortalAlert[]>('/api/monitoring/alerts');
  return data;
}

// --- Checklist ---
export async function generateChecklist(tenderId: number): Promise<ChecklistItem[]> {
  const { data } = await api.post<ChecklistItem[]>(`/api/tenders/${tenderId}/checklist/generate`);
  return data;
}

export async function getChecklist(tenderId: number): Promise<ChecklistItem[]> {
  const { data } = await api.get<ChecklistItem[]>(`/api/tenders/${tenderId}/checklist/`);
  return data;
}

export async function getChecklistCompletion(tenderId: number): Promise<ChecklistCompletion> {
  const { data } = await api.get<ChecklistCompletion>(`/api/tenders/${tenderId}/checklist/completion`);
  return data;
}

export async function uploadChecklistDocument(tenderId: number, itemId: number, file: File): Promise<ChecklistItem> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await api.post<ChecklistItem>(
    `/api/tenders/${tenderId}/checklist/${itemId}/upload`,
    formData,
    { headers: { 'Content-Type': undefined } }
  );
  return data;
}

export async function deleteChecklistDocument(tenderId: number, itemId: number): Promise<void> {
  await api.delete(`/api/tenders/${tenderId}/checklist/${itemId}/document`);
}

export async function addChecklistItem(tenderId: number, item: { item_name: string; item_description?: string; is_required?: boolean }): Promise<ChecklistItem> {
  const { data } = await api.post<ChecklistItem>(`/api/tenders/${tenderId}/checklist/items`, item);
  return data;
}

export async function generateChecklistDocuments(tenderId: number): Promise<{ triggered: number }> {
  const { data } = await api.post<{ triggered: number }>(`/api/tenders/${tenderId}/checklist/generate-documents`);
  return data;
}

export async function generateChecklistItemDocument(tenderId: number, itemId: number): Promise<ChecklistItem> {
  const { data } = await api.post<ChecklistItem>(`/api/tenders/${tenderId}/checklist/${itemId}/generate`);
  return data;
}

export async function previewChecklistItemDocument(tenderId: number, itemId: number): Promise<Blob> {
  const { data } = await api.get<Blob>(`/api/tenders/${tenderId}/checklist/${itemId}/preview`, {
    responseType: 'blob',
  });
  return data;
}

/**
 * Live PDF preview of a workspace document's *draft* (no GeneratedDocument
 * needed). Use this from the workspace editor's preview pane — it pulls
 * content + letterhead + signatures directly off the DocumentWorkspace row.
 * previewChecklistItemDocument above only works after Finalize.
 *
 * Also returns the authoritative page count from the rendered PDF
 * (`X-PDF-Page-Count` header) so the signature placement board can show
 * page boundaries that match the actual PDF — letterhead margins cause the
 * natural-HTML page estimate to diverge from WeasyPrint pagination.
 */
export async function previewWorkspaceDocument(
  tenderId: number,
  itemId: number,
): Promise<{ blob: Blob; pageCount: number | null }> {
  const response = await api.get<Blob>(
    `/api/tenders/${tenderId}/workspace/items/${itemId}/preview-pdf`,
    { responseType: 'blob' },
  );
  const hdr = (response.headers as any)['x-pdf-page-count'];
  const parsed = hdr != null ? Number(hdr) : NaN;
  return {
    blob: response.data,
    pageCount: Number.isFinite(parsed) && parsed > 0 ? parsed : null,
  };
}

/**
 * Workspace preview rasterised to a single tall PNG (all pages stacked
 * vertically), for the signature placement board. Renders the same
 * letterhead-merged PDF as previewWorkspaceDocument so signature
 * coordinates dropped on the board land at the same content position in
 * the final PDF. Returns pixel dimensions so the client can compute page
 * boundaries without re-measuring.
 */
export async function previewWorkspacePages(
  tenderId: number,
  itemId: number,
): Promise<{
  blob: Blob;
  pageCount: number | null;
  pageHeightPx: number | null;
  pageWidthPx: number | null;
}> {
  const response = await api.get<Blob>(
    `/api/tenders/${tenderId}/workspace/items/${itemId}/preview-pages.png`,
    { responseType: 'blob' },
  );
  const headers = response.headers as any;
  const parseHeader = (k: string): number | null => {
    const v = headers[k];
    if (v == null) return null;
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  return {
    blob: response.data,
    pageCount: parseHeader('x-pdf-page-count'),
    pageHeightPx: parseHeader('x-image-page-height-px'),
    pageWidthPx: parseHeader('x-image-page-width-px'),
  };
}

export async function updateChecklistItemCategory(tenderId: number, itemId: number, category: string): Promise<ChecklistItem> {
  const { data } = await api.patch<ChecklistItem>(`/api/tenders/${tenderId}/checklist/${itemId}/category`, { item_category: category });
  return data;
}

// --- Proposal Reviews (Admin) ---
export async function getPendingReviews(): Promise<PendingReview[]> {
  const { data } = await api.get<PendingReview[]>('/api/proposals/reviews');
  return data;
}

export async function approveReview(reviewId: number, comments: string = ''): Promise<void> {
  await api.post(`/api/proposals/reviews/${reviewId}/approve`, { comments });
}

export async function rejectReview(reviewId: number, comments: string = ''): Promise<void> {
  await api.post(`/api/proposals/reviews/${reviewId}/reject`, { comments });
}

export async function requestReviewChanges(reviewId: number, comments: string = ''): Promise<void> {
  await api.post(`/api/proposals/reviews/${reviewId}/request-changes`, { comments });
}

// --- Admin: Settings ---
export async function getAdminSettings(): Promise<any[]> {
  const { data } = await api.get('/api/admin/settings/');
  return data;
}

export async function updateAdminSetting(key: string, value: any): Promise<any> {
  const { data } = await api.put(`/api/admin/settings/${key}`, { value });
  return data;
}

export async function resetAdminSetting(key: string): Promise<any> {
  const { data } = await api.post(`/api/admin/settings/reset/${key}`);
  return data;
}

// --- Admin: Tender Scope Profile (Phase 7) ---
export interface ScopeKeywordGroup {
  label: string;
  keywords: string[];
}

export interface TenderScopeProfile {
  id: number;
  name: string;
  keyword_groups: ScopeKeywordGroup[];
  exclusion_terms: string[];
  target_ministries: string[];
  value_min: number | null;
  value_max: number | null;
  relevance_threshold: number;
  is_active: boolean;
  updated_by: number | null;
  updated_at: string | null;
  created_at: string | null;
}

export async function getScopeProfile(): Promise<TenderScopeProfile> {
  const { data } = await api.get('/api/admin/scope-profile/');
  return data;
}

export async function updateScopeProfile(
  payload: Partial<Omit<TenderScopeProfile, 'id' | 'name' | 'updated_by' | 'updated_at' | 'created_at'>>,
): Promise<TenderScopeProfile> {
  const { data } = await api.put('/api/admin/scope-profile/', payload);
  return data;
}

// --- Admin: Tender Scoring Agent (auto-tender-scoring) ---
export async function getScoringSettings(): Promise<Record<string, any>> {
  const { data } = await api.get('/api/admin/tender-scoring/settings');
  return data as Record<string, any>;
}

export async function updateScoringSettings(
  body: Record<string, any>,
  resegment = false,
): Promise<Record<string, any>> {
  const { data } = await api.put('/api/admin/tender-scoring/settings', body, { params: { resegment } });
  return data as Record<string, any>;
}

export async function getScoringStats(): Promise<{
  total_scored: number;
  unscored_backlog: number;
  last_reaper_run: string | null;
  counts: { to_bid: number; not_bidable: number; discarded: number; unscored: number };
}> {
  const { data } = await api.get('/api/admin/tender-scoring/stats');
  return data;
}

export async function getScoringDigest(): Promise<{ digest: string | null }> {
  const { data } = await api.get('/api/admin/tender-scoring/digest');
  return data as { digest: string | null };
}

export async function regenerateScoringDigest(): Promise<{ digest: string }> {
  const { data } = await api.post('/api/admin/tender-scoring/digest/regenerate');
  return data as { digest: string };
}

export interface ScoringBacklog {
  total: number;
  scored: number;
  pending: number;
  drainable: number;
  stuck_at_cap: number;
  in_flight_batches: number;
  last_reaper_run: string | null;
  enabled: boolean;
  est_cost_inr: number;
}

export async function getScoringBacklog(): Promise<ScoringBacklog> {
  const { data } = await api.get('/api/admin/tender-scoring/backlog');
  return data as ScoringBacklog;
}

export async function drainScoring(
  mode: 'live' | 'batch' = 'live',
  limit?: number,
): Promise<any> {
  const { data } = await api.post('/api/admin/tender-scoring/drain', { mode, limit });
  return data;
}

export interface RecentCosting {
  tender_id: number;
  title: string;
  grand_total: number | null;
  margin_pct: number | null;
  status: string;
  updated_at: string | null;
}

export async function getRecentCostings(limit = 5): Promise<RecentCosting[]> {
  const { data } = await api.get('/api/cost-breakdowns/recent', { params: { limit } });
  return data as RecentCosting[];
}

// --- Admin: Users ---
export async function getAdminUsers(params?: Record<string, any>): Promise<any[]> {
  const { data } = await api.get('/api/admin/users/', { params });
  return data;
}

export async function createAdminUser(body: { email: string; name: string; password: string; role?: string }): Promise<any> {
  const { data } = await api.post('/api/admin/users/', body);
  return data;
}

export async function updateAdminUser(userId: number, body: { name?: string; email?: string; role?: string }): Promise<any> {
  const { data } = await api.put(`/api/admin/users/${userId}`, body);
  return data;
}

export async function activateAdminUser(userId: number): Promise<void> {
  await api.patch(`/api/admin/users/${userId}/activate`);
}

export async function deactivateAdminUser(userId: number): Promise<void> {
  await api.patch(`/api/admin/users/${userId}/deactivate`);
}

export async function resetAdminUserPassword(userId: number, newPassword: string): Promise<void> {
  await api.post(`/api/admin/users/${userId}/reset-password`, { new_password: newPassword });
}

// --- Admin: Security / Redaction Rules ---
export async function getRedactionRules(): Promise<any[]> {
  const { data } = await api.get('/api/admin/security/redaction-rules');
  return data;
}

export async function createRedactionRule(body: { name: string; pattern: string; replacement: string; category?: string; description?: string }): Promise<any> {
  const { data } = await api.post('/api/admin/security/redaction-rules', body);
  return data;
}

export async function updateRedactionRule(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/admin/security/redaction-rules/${id}`, body);
  return data;
}

export async function deleteRedactionRule(id: number): Promise<void> {
  await api.delete(`/api/admin/security/redaction-rules/${id}`);
}

export async function testRedactionPattern(pattern: string, sampleText: string): Promise<any> {
  const { data } = await api.post('/api/admin/security/redaction-rules/test', { pattern, sample_text: sampleText });
  return data;
}

// --- Admin: Retention Policies ---
export async function getRetentionPolicies(): Promise<any[]> {
  const { data } = await api.get('/api/admin/security/retention-policies');
  return data;
}

export async function updateRetentionPolicy(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/admin/security/retention-policies/${id}`, body);
  return data;
}

// --- Admin: Audit Logs ---
export async function getAuditLogs(params?: Record<string, any>): Promise<any[]> {
  const { data } = await api.get('/api/admin/audit-logs/', { params });
  return data;
}

export async function getAuditActions(): Promise<string[]> {
  const { data } = await api.get('/api/admin/audit-logs/actions');
  return data;
}

export async function exportAuditLogs(params?: Record<string, any>): Promise<Blob> {
  const { data } = await api.get('/api/admin/audit-logs/export', { params, responseType: 'blob' });
  return data;
}

// --- Admin: Dashboard ---
export async function getAdminDashboard(): Promise<any> {
  const { data } = await api.get('/api/admin/dashboard/overview');
  return data;
}

export async function getAdminApiUsage(days: number = 30): Promise<any> {
  const { data } = await api.get(`/api/admin/dashboard/api-usage?days=${days}`);
  return data;
}

export async function getAdminHealth(): Promise<any> {
  const { data } = await api.get('/api/admin/dashboard/health');
  return data;
}

export async function getAdminActiveUsers(): Promise<any[]> {
  const { data } = await api.get('/api/admin/dashboard/active-users');
  return data;
}

// --- Templates ---
export async function getTemplates(): Promise<any[]> {
  const { data } = await api.get('/api/templates');
  return data;
}

export async function uploadTemplate(file: File): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await api.post('/api/templates/upload', formData, {
    headers: { 'Content-Type': undefined },
  });
  return data;
}

export async function updateTemplate(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/templates/${id}`, body);
  return data;
}

export async function deleteTemplate(id: number): Promise<void> {
  await api.delete(`/api/templates/${id}`);
}

export async function setSessionTemplate(sessionId: number, templateId: number): Promise<any> {
  const { data } = await api.put(`/api/proposals/sessions/${sessionId}/template`, { template_id: templateId });
  return data;
}

// --- Unified Template Management ---
export async function getUnifiedTemplates(kind?: string, category?: string, zone?: string): Promise<any[]> {
  const params = new URLSearchParams();
  if (kind) params.set('kind', kind);
  if (category) params.set('category', category);
  if (zone) params.set('zone', zone);
  const { data } = await api.get(`/api/templates/unified?${params.toString()}`);
  return data;
}

export async function uploadTemplateUnified(file: File, templateKind: string, category?: string): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const params = new URLSearchParams({ template_kind: templateKind });
  if (category) params.set('category', category);
  const { data } = await api.post(`/api/templates/upload/unified?${params.toString()}`, formData, {
    headers: { 'Content-Type': undefined },
  });
  return data;
}

export async function getTemplatePreview(kind: string, templateId: number): Promise<any> {
  const { data } = await api.get(`/api/templates/${kind}/${templateId}/preview`);
  return data;
}

// Document format templates
export async function getDocumentTemplates(): Promise<any[]> {
  const { data } = await api.get('/api/templates/document/');
  return data;
}

export async function createDocumentTemplate(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/templates/document/', body);
  return data;
}

export async function updateDocumentTemplate(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/templates/document/${id}`, body);
  return data;
}

export async function deleteDocumentTemplate(id: number): Promise<void> {
  await api.delete(`/api/templates/document/${id}`);
}

// Costing templates
export async function getCostingTemplates(zone?: string): Promise<any[]> {
  const params = zone ? `?zone=${zone}` : '';
  const { data } = await api.get(`/api/templates/costing/${params}`);
  return data;
}

export async function createCostingTemplate(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/templates/costing/', body);
  return data;
}

export async function updateCostingTemplate(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/templates/costing/${id}`, body);
  return data;
}

export async function deleteCostingTemplate(id: number): Promise<void> {
  await api.delete(`/api/templates/costing/${id}`);
}

// --- Document Analysis ---
export interface BatchAnalysisResult {
  tender_id: number;
  status: 'completed' | 'failed' | 'skipped' | 'in_progress';
  total_requirements?: number;
  total_critical_flags?: number;
  completeness_score?: number | null;
  documents_analyzed?: number;
  error?: string;
}

export interface BatchAnalysisResponse {
  batch_size: number;
  succeeded: number;
  failed: number;
  skipped: number;
  results: BatchAnalysisResult[];
}

export async function triggerBatchTenderAnalysis(
  tenderIds: number[],
): Promise<BatchAnalysisResponse> {
  // Long-running: the backend blocks until all tenders finish. Extend timeout
  // to 15 min so axios doesn't kill the request for a realistic batch.
  const { data } = await api.post(
    `/api/tenders/analysis/batch`,
    { tender_ids: tenderIds },
    { timeout: 15 * 60 * 1000 },
  );
  return data;
}

// --- Letterhead Templates ---
export async function getLetterheadTemplates(): Promise<any[]> {
  const { data } = await api.get('/api/letterhead/templates');
  return data;
}

export async function createLetterheadTemplate(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/letterhead/templates', body);
  return data;
}

export async function updateLetterheadTemplate(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/letterhead/templates/${id}`, body);
  return data;
}

export async function deleteLetterheadTemplate(id: number): Promise<void> {
  await api.delete(`/api/letterhead/templates/${id}`);
}

export async function uploadLetterheadAsset(templateId: number, assetType: string, file: File): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await api.post(`/api/letterhead/templates/${templateId}/upload/${assetType}`, formData, {
    headers: { 'Content-Type': undefined },
  });
  return data;
}

export async function uploadLetterheadPdf(templateId: number, file: File): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await api.post(`/api/letterhead/templates/${templateId}/upload-pdf`, formData, {
    headers: { 'Content-Type': undefined },
  });
  return data;
}

export async function previewLetterhead(templateId: number): Promise<Blob> {
  const { data } = await api.get(`/api/letterhead/templates/${templateId}/preview`, { responseType: 'blob' });
  return data;
}

// --- Digital Signatures ---
export async function getSignatures(): Promise<any[]> {
  const { data } = await api.get('/api/signatures/');
  return data;
}

/**
 * Fetch a signature/stamp image as a base64 data URI for inline embedding
 * inside the workspace editor. Self-contained so saved document HTML keeps
 * rendering the same image even if the signature record changes later.
 */
export async function getSignatureImageDataUri(
  signatureId: number,
  kind: 'signature' | 'stamp' = 'signature',
): Promise<{ data_uri: string; name: string; designation: string | null; mime: string }> {
  const { data } = await api.get(
    `/api/signatures/${signatureId}/image-data-uri`,
    { params: { kind } },
  );
  return data;
}

export async function getAllSignatures(): Promise<any[]> {
  const { data } = await api.get('/api/signatures/all');
  return data;
}

export async function createSignature(formData: FormData): Promise<any> {
  const { data } = await api.post('/api/signatures/', formData, {
    headers: { 'Content-Type': undefined },
  });
  return data;
}

export async function deleteSignature(id: number): Promise<void> {
  await api.delete(`/api/signatures/${id}`);
}

// --- Generated Documents ---
export async function createDocument(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/documents/', body);
  return data;
}

export async function getDocuments(): Promise<any[]> {
  const { data } = await api.get('/api/documents/');
  return data;
}

export async function getDocument(id: number): Promise<any> {
  const { data } = await api.get(`/api/documents/${id}`);
  return data;
}

export async function updateDocument(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/documents/${id}`, body);
  return data;
}

export async function deleteDocument(id: number): Promise<any> {
  const { data } = await api.delete(`/api/documents/${id}`);
  return data;
}

export async function bulkDeleteDocuments(documentIds: number[]): Promise<any> {
  const { data } = await api.post('/api/documents/bulk-delete', { document_ids: documentIds });
  return data;
}

export async function generateDocumentPdf(id: number): Promise<any> {
  const { data } = await api.post(`/api/documents/${id}/generate`);
  return data;
}

export async function previewDocument(id: number): Promise<Blob> {
  const { data } = await api.get(`/api/documents/${id}/preview`, { responseType: 'blob' });
  return data;
}

export async function downloadDocument(id: number): Promise<Blob> {
  const { data } = await api.get(`/api/documents/${id}/download`, { responseType: 'blob' });
  return data;
}

export async function createDocumentFromProposal(sessionId: number): Promise<any> {
  const { data } = await api.post(`/api/documents/from-proposal/${sessionId}`);
  return data;
}

export async function getDocumentTypeTemplate(type: string): Promise<any> {
  const { data } = await api.get(`/api/documents/templates/${type}`);
  return data;
}

export async function aiGenerateDocumentContent(
  id: number,
  body: { prompt: string; mode: string; document_type?: string }
): Promise<{ content: string }> {
  const { data } = await api.post(`/api/documents/${id}/ai-generate`, body);
  return data;
}

// --- Offline Document Signing ---
// Upload an offline PDF, place existing platform signatures/stamps on it,
// apply, and download. Distinct from Generated Documents (TipTap + letterhead).

export interface OfflineDocument {
  id: number;
  title: string;
  document_type: string;
  status: string; // 'draft' | 'signed'
  signatures: SignatureConfig[];
  has_signed_output: boolean;
  file_size: number | null;
  created_at: string | null;
  updated_at: string | null;
  page_count?: number | null;
}

export interface SignatureConfig {
  signature_id: number;
  position: string;
  position_x?: number;
  position_y?: number;
  page: string | number;
}

export async function getOfflineDocuments(): Promise<OfflineDocument[]> {
  const { data } = await api.get('/api/offline-documents/');
  return data;
}

export async function getOfflineDocument(id: number): Promise<OfflineDocument> {
  const { data } = await api.get(`/api/offline-documents/${id}`);
  return data;
}

export async function uploadOfflineDocument(file: File): Promise<OfflineDocument> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await api.post('/api/offline-documents/upload', form, {
    headers: { 'Content-Type': undefined },
  });
  return data;
}

export async function updateOfflineDocumentPlacement(
  id: number,
  body: { signatures?: SignatureConfig[]; title?: string },
): Promise<OfflineDocument> {
  const { data } = await api.patch(`/api/offline-documents/${id}`, body);
  return data;
}

export async function applyOfflineSignatures(id: number): Promise<OfflineDocument> {
  const { data } = await api.post(`/api/offline-documents/${id}/apply`);
  return data;
}

export async function downloadOfflineDocument(id: number): Promise<Blob> {
  const { data } = await api.get(`/api/offline-documents/${id}/download`, {
    responseType: 'blob',
  });
  return data;
}

export async function deleteOfflineDocument(id: number): Promise<void> {
  await api.delete(`/api/offline-documents/${id}`);
}

/**
 * Rasterised source PDF (all pages stacked into one tall PNG) for the
 * signature placement board. Returns pixel dimensions + page count from
 * response headers so the board lines drag positions up with the final PDF.
 * Mirrors previewWorkspacePages.
 */
export async function getOfflinePreviewPages(id: number): Promise<{
  blob: Blob;
  pageCount: number | null;
  pageHeightPx: number | null;
  pageWidthPx: number | null;
}> {
  const response = await api.get<Blob>(
    `/api/offline-documents/${id}/preview-pages.png`,
    { responseType: 'blob' },
  );
  const headers = response.headers as any;
  const parseHeader = (k: string): number | null => {
    const v = headers[k];
    if (v == null) return null;
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  return {
    blob: response.data,
    pageCount: parseHeader('x-pdf-page-count'),
    pageHeightPx: parseHeader('x-image-page-height-px'),
    pageWidthPx: parseHeader('x-image-page-width-px'),
  };
}

// --- Agent Builder ---
export async function getBuilderAgents(params?: Record<string, any>): Promise<any[]> {
  const { data } = await api.get('/api/agent-builder/agents', { params });
  return data;
}

export async function createBuilderAgent(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/agent-builder/agents', body);
  return data;
}

export async function getBuilderAgent(id: number): Promise<any> {
  const { data } = await api.get(`/api/agent-builder/agents/${id}`);
  return data;
}

export async function updateBuilderAgent(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/agent-builder/agents/${id}`, body);
  return data;
}

export async function deleteBuilderAgent(id: number): Promise<void> {
  await api.delete(`/api/agent-builder/agents/${id}`);
}

export async function cloneBuilderAgent(id: number, newName: string): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${id}/clone`, { new_name: newName });
  return data;
}

export async function publishBuilderAgent(id: number): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${id}/publish`);
  return data;
}

export async function getAgentVersions(id: number): Promise<any[]> {
  const { data } = await api.get(`/api/agent-builder/agents/${id}/versions`);
  return data;
}

export async function rollbackAgent(id: number, version: number): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${id}/rollback/${version}`);
  return data;
}

// ─── Phase 3d — Canonical / reset / validate (Agent Builder authoritative) ───

export interface CanonicalAgentResponse {
  agent_key: string;
  registered: boolean;
  canonical_prompt?: string | null;
  canonical_tools?: string[];
  required_placeholders?: string[];
  soft_placeholders?: string[];
  supports_user_prompt?: boolean;
  supports_user_tools?: boolean;
  current_is_user_customized?: boolean;
  current_prompt_matches_canonical?: boolean;
  message?: string;
}

export async function getAgentCanonical(id: number): Promise<CanonicalAgentResponse> {
  const { data } = await api.get(`/api/agent-builder/agents/${id}/canonical`);
  return data;
}

export interface PromptValidationResponse {
  valid: boolean;
  missing_placeholders: string[];
  missing_soft?: string[];
  warnings: string[];
  registered?: boolean;
}

export async function validateAgentPrompt(
  id: number,
  systemPrompt: string,
): Promise<PromptValidationResponse> {
  const { data } = await api.post(`/api/agent-builder/agents/${id}/validate-prompt`, {
    system_prompt: systemPrompt,
  });
  return data;
}

export async function resetAgentToDefault(
  id: number,
  fields: Array<'system_prompt' | 'tools'> = ['system_prompt', 'tools'],
): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${id}/reset-to-default`, { fields });
  return data;
}

export async function executeBuilderAgent(id: number, inputData: Record<string, any>): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${id}/execute`, { input_data: inputData });
  return data;
}

export async function executeBuilderAgentWithFiles(
  id: number,
  inputData: Record<string, any>,
  files: File[],
  docIds: number[] = [],
): Promise<any> {
  const formData = new FormData();
  formData.append('input_json', JSON.stringify(inputData));
  for (const file of files) {
    formData.append('files', file);
  }
  if (docIds.length > 0) {
    formData.append('doc_ids', docIds.join(','));
  }
  const { data } = await api.post(`/api/agent-builder/agents/${id}/execute-with-files`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

// --- Test Documents ---

export async function uploadTestDocuments(agentId: number, files: File[]): Promise<any> {
  const formData = new FormData();
  for (const file of files) {
    formData.append('files', file);
  }
  const { data } = await api.post(`/api/agent-builder/agents/${agentId}/test-documents`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function getTestDocuments(agentId: number): Promise<any[]> {
  const { data } = await api.get(`/api/agent-builder/agents/${agentId}/test-documents`);
  return data;
}

export async function getTestDocument(docId: number): Promise<any> {
  const { data } = await api.get(`/api/agent-builder/test-documents/${docId}`);
  return data;
}

export async function deleteTestDocument(docId: number): Promise<any> {
  const { data } = await api.delete(`/api/agent-builder/test-documents/${docId}`);
  return data;
}

export async function deleteAllTestDocuments(agentId: number): Promise<any> {
  const { data } = await api.delete(`/api/agent-builder/agents/${agentId}/test-documents`);
  return data;
}

export async function reExtractTestDocument(docId: number): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/test-documents/${docId}/re-extract`);
  return data;
}

export async function getAgentExecutions(id: number): Promise<any[]> {
  const { data } = await api.get(`/api/agent-builder/agents/${id}/executions`);
  return data;
}

export async function getAgentExecutionDetail(executionId: number): Promise<any> {
  const { data } = await api.get(`/api/agent-builder/executions/${executionId}`);
  return data;
}

export async function getAgentStats(id: number): Promise<any> {
  const { data } = await api.get(`/api/agent-builder/agents/${id}/stats`);
  return data;
}

export async function getAgentTests(agentId: number): Promise<any[]> {
  const { data } = await api.get(`/api/agent-builder/agents/${agentId}/tests`);
  return data;
}

export async function createAgentTest(agentId: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${agentId}/tests`, body);
  return data;
}

export async function updateAgentTest(testId: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/agent-builder/tests/${testId}`, body);
  return data;
}

export async function deleteAgentTest(testId: number): Promise<void> {
  await api.delete(`/api/agent-builder/tests/${testId}`);
}

export async function runAgentTest(testId: number): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/tests/${testId}/run`);
  return data;
}

export async function runAllAgentTests(agentId: number): Promise<any[]> {
  const { data } = await api.post(`/api/agent-builder/agents/${agentId}/tests/run-all`);
  return data;
}

export async function compareAgentVersions(agentId: number, versionA: number, versionB: number): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/agents/${agentId}/compare-versions`, { version_a: versionA, version_b: versionB });
  return data;
}

export async function getBuilderTools(toolType?: string): Promise<any[]> {
  const params = toolType ? { tool_type: toolType } : {};
  const { data } = await api.get('/api/agent-builder/tools', { params });
  return data;
}

export async function createBuilderTool(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/agent-builder/tools', body);
  return data;
}

export async function updateBuilderTool(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/agent-builder/tools/${id}`, body);
  return data;
}

export async function deleteBuilderTool(id: number): Promise<void> {
  await api.delete(`/api/agent-builder/tools/${id}`);
}

export async function testBuilderTool(toolId: number, input: any): Promise<any> {
  const { data } = await api.post(`/api/agent-builder/tools/${toolId}/test`, { input });
  return data;
}

export async function getAgentLibrary(): Promise<any[]> {
  const { data } = await api.get('/api/agent-builder/library');
  return data;
}

// --- LangChain Pipeline ---
export async function runTenderPipeline(tenderId: number, body?: Record<string, any>): Promise<any> {
  const { data } = await api.post(`/api/langchain/pipeline/${tenderId}/run`, body || {});
  return data;
}

export async function runPipelineStep(tenderId: number, step: string, body?: Record<string, any>): Promise<any> {
  const { data } = await api.post(`/api/langchain/pipeline/${tenderId}/run-step`, { step, ...body });
  return data;
}

export async function getPipelineStatus(tenderId: number): Promise<any> {
  const { data } = await api.get(`/api/langchain/pipeline/${tenderId}/status`);
  return data;
}

// --- LangChain Agent Chat ---
export async function chatWithAgent(agentKey: string, message: string, sessionId?: string): Promise<any> {
  const { data } = await api.post(`/api/langchain/agents/${agentKey}/chat`, { message, session_id: sessionId });
  return data;
}

export async function getAgentChatHistory(agentKey: string, sessionId: string): Promise<any[]> {
  const { data } = await api.get(`/api/langchain/agents/${agentKey}/chat/${sessionId}/history`);
  return data;
}

// --- Agent Memory ---
export async function getAgentMemories(params?: Record<string, any>): Promise<any[]> {
  const { data } = await api.get('/api/langchain/memory', { params });
  return data;
}

export async function createAgentMemory(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/langchain/memory', body);
  return data;
}

export async function deleteAgentMemory(id: number): Promise<void> {
  await api.delete(`/api/langchain/memory/${id}`);
}

export async function searchAgentMemories(query: string, agentKey?: string): Promise<any[]> {
  const params: Record<string, any> = { query };
  if (agentKey) params.agent_key = agentKey;
  const { data } = await api.get('/api/langchain/memory/search', { params });
  return data;
}

export async function getMemoryStats(): Promise<any> {
  const { data } = await api.get('/api/langchain/memory/stats');
  return data;
}

export async function parseMemoryMd(file: File, agentKey?: string, useAi?: boolean): Promise<{ entries: any[]; count: number; file_name: string }> {
  const formData = new FormData();
  formData.append('file', file);
  if (agentKey) formData.append('agent_key', agentKey);
  if (useAi) formData.append('use_ai', 'true');
  const { data } = await api.post('/api/langchain/memory/parse-md', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function commitMemoryUpload(entries: any[], agentKey?: string): Promise<{ created_ids: number[]; count: number }> {
  const { data } = await api.post('/api/langchain/memory/upload-md', { entries, agent_key: agentKey });
  return data;
}

// --- MCP Servers ---
export async function getMCPServers(): Promise<any[]> {
  const { data } = await api.get('/api/langchain/mcp/servers');
  return data;
}

export async function addMCPServer(body: Record<string, any>): Promise<any> {
  const { data } = await api.post('/api/langchain/mcp/servers', body);
  return data;
}

export async function updateMCPServer(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/langchain/mcp/servers/${id}`, body);
  return data;
}

export async function deleteMCPServer(id: number): Promise<void> {
  await api.delete(`/api/langchain/mcp/servers/${id}`);
}

export async function testMCPServer(id: number): Promise<any> {
  const { data } = await api.post(`/api/langchain/mcp/servers/${id}/test`);
  return data;
}

export async function getMCPServerTools(id: number): Promise<any> {
  const { data } = await api.get(`/api/langchain/mcp/servers/${id}/tools`);
  return data;
}

// ── Training Datasets ──

export async function getTrainingDatasets(status?: string): Promise<any[]> {
  const params: Record<string, string> = {};
  if (status) params.status = status;
  const { data } = await api.get('/api/training-datasets/', { params });
  return data;
}

export async function createTrainingDataset(body: { name: string; description?: string; tags?: string[] }): Promise<any> {
  const { data } = await api.post('/api/training-datasets/', body);
  return data;
}

export async function getTrainingDataset(id: number): Promise<any> {
  const { data } = await api.get(`/api/training-datasets/${id}`);
  return data;
}

export async function updateTrainingDataset(id: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/training-datasets/${id}`, body);
  return data;
}

export async function deleteTrainingDataset(id: number): Promise<void> {
  await api.delete(`/api/training-datasets/${id}`);
}

export async function uploadTrainingDatasetFiles(datasetId: number, files: File[]): Promise<any> {
  const formData = new FormData();
  files.forEach(f => formData.append('files', f));
  const { data } = await api.post(`/api/training-datasets/${datasetId}/files`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function deleteTrainingDatasetFile(fileId: number): Promise<void> {
  await api.delete(`/api/training-datasets/files/${fileId}`);
}

export async function getTrainingDatasetFileContent(fileId: number): Promise<any> {
  const { data } = await api.get(`/api/training-datasets/files/${fileId}/content`);
  return data;
}

export async function previewTrainingDatasetContext(id: number): Promise<{ context: string; char_count: number; estimated_tokens: number }> {
  const { data } = await api.get(`/api/training-datasets/${id}/preview`);
  return data;
}

export async function getAgentTrainingDatasets(agentId: number): Promise<any[]> {
  const { data } = await api.get(`/api/training-datasets/agent/${agentId}`);
  return data;
}

export async function assignDatasetToAgent(datasetId: number, agentId: number, priority: number = 0): Promise<any> {
  const { data } = await api.post(`/api/training-datasets/${datasetId}/assign/${agentId}`, { priority });
  return data;
}

export async function unassignDatasetFromAgent(datasetId: number, agentId: number): Promise<void> {
  await api.delete(`/api/training-datasets/${datasetId}/assign/${agentId}`);
}

export async function updateDatasetPriority(datasetId: number, agentId: number, priority: number): Promise<void> {
  await api.put(`/api/training-datasets/${datasetId}/assign/${agentId}/priority`, { priority });
}

// ── Ratecards (structured OEM/Fleetguard price lists for component costing) ──

export interface Ratecard {
  id: number;
  name: string;
  source_label: string | null;
  description: string | null;
  status: string;
  original_file_name: string | null;
  item_count: number;
  check_schedule_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface RatecardCheckSchedule {
  id: number;
  engine_type: string | null;
  check_level: string | null;
  name: string | null;
}

export interface RatecardDetail {
  id: number;
  name: string;
  source_label: string | null;
  description: string | null;
  status: string;
  original_file_name: string | null;
  item_count: number;
  check_schedules: RatecardCheckSchedule[];
}

export interface RatecardItem {
  part_no: string | null;
  description: string | null;
  qty: number | null;
  uom: string | null;
  rate: number | null;
  source: string | null;
  engine_type: string | null;
  is_mandatory: boolean;
  annexure: string | null;
}

export interface RatecardIngestResult {
  ratecard_id: number;
  items: number;
  check_schedules: number;
  skipped: string[];
}

export interface RatecardPreview {
  item_count: number;
  check_schedule_count: number;
  check_schedules: { engine_type: string | null; check_level: string | null; name: string | null }[];
  sample_items: RatecardItem[];
  skipped: string[];
}

export async function listRatecards(status?: string): Promise<Ratecard[]> {
  const params: Record<string, string> = {};
  if (status) params.status = status;
  const { data } = await api.get('/api/ratecards/', { params });
  return data;
}

export async function createRatecard(body: { name: string; source_label?: string; description?: string }): Promise<Ratecard> {
  const { data } = await api.post('/api/ratecards/', body);
  return data;
}

export async function getRatecard(id: number): Promise<RatecardDetail> {
  const { data } = await api.get(`/api/ratecards/${id}`);
  return data;
}

export async function deleteRatecard(id: number): Promise<void> {
  await api.delete(`/api/ratecards/${id}`);
}

export async function uploadRatecardFile(ratecardId: number, file: File): Promise<RatecardIngestResult> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await api.post(`/api/ratecards/${ratecardId}/upload`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function previewRatecardFile(file: File): Promise<RatecardPreview> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await api.post('/api/ratecards/preview', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function listRatecardItems(
  ratecardId: number,
  opts: { engine_type?: string; check_level?: string; limit?: number; offset?: number } = {},
): Promise<RatecardItem[]> {
  const { data } = await api.get(`/api/ratecards/${ratecardId}/items`, { params: opts });
  return data;
}

export async function deleteRatecardItem(itemId: number): Promise<void> {
  await api.delete(`/api/ratecards/items/${itemId}`);
}

// ── Proposal Chat (Agent Router) ──

export async function proposalChat(body: { message: string; session_id?: string; tender_id?: number }): Promise<any> {
  const { data } = await api.post('/api/langchain/proposal-chat', body);
  return data;
}

export async function getProposalChatHistory(sessionId: string): Promise<any[]> {
  const { data } = await api.get(`/api/langchain/proposal-chat/${sessionId}/history`);
  return data;
}

// ── Command Center ──

export async function createCommandCenterSession(body: { tender_id?: number; title?: string; context?: string }): Promise<any> {
  const { data } = await api.post('/api/command-center/sessions', body);
  return data;
}

export async function listCommandCenterSessions(): Promise<any[]> {
  const { data } = await api.get('/api/command-center/sessions');
  return data;
}

export async function getCommandCenterSession(sessionId: number): Promise<any> {
  const { data } = await api.get(`/api/command-center/sessions/${sessionId}`);
  return data;
}

export async function updateCommandCenterSession(sessionId: number, body: { tender_id?: number; title?: string }): Promise<any> {
  const { data } = await api.patch(`/api/command-center/sessions/${sessionId}`, body);
  return data;
}

export async function deleteCommandCenterSession(sessionId: number): Promise<any> {
  const { data } = await api.delete(`/api/command-center/sessions/${sessionId}`);
  return data;
}

export async function bulkDeleteCommandCenterSessions(sessionIds: number[]): Promise<any> {
  const { data } = await api.post('/api/command-center/sessions/bulk-delete', { session_ids: sessionIds });
  return data;
}

export async function bulkArchiveTenders(ids: number[]): Promise<{ archived: number }> {
  const { data } = await api.post('/api/tenders/bulk-archive', { tender_ids: ids });
  return data;
}

export async function bulkDeleteTenders(ids: number[]): Promise<{ deleted: number }> {
  const { data } = await api.post('/api/tenders/bulk-delete', { tender_ids: ids });
  return data;
}

export async function getCommandCenterHistory(sessionId: number, limit = 50): Promise<any[]> {
  const { data } = await api.get(`/api/command-center/sessions/${sessionId}/history?limit=${limit}`);
  return data;
}

export async function getSessionArtifacts(sessionId: number, artifactType?: string): Promise<any[]> {
  const params = artifactType ? `?artifact_type=${artifactType}` : '';
  const { data } = await api.get(`/api/command-center/sessions/${sessionId}/artifacts${params}`);
  return data;
}

export async function getArtifact(artifactId: number): Promise<any> {
  const { data } = await api.get(`/api/command-center/artifacts/${artifactId}`);
  return data;
}

export async function updateArtifact(artifactId: number, body: { content: string; structured_data?: any }): Promise<any> {
  const { data } = await api.put(`/api/command-center/artifacts/${artifactId}`, body);
  return data;
}

export async function getArtifactVersions(artifactId: number): Promise<any[]> {
  const { data } = await api.get(`/api/command-center/artifacts/${artifactId}/versions`);
  return data;
}

export async function exportArtifact(artifactId: number, format = 'docx'): Promise<Blob> {
  const { data } = await api.post(`/api/command-center/artifacts/${artifactId}/export`, { format }, { responseType: 'blob' });
  return data;
}

export async function getSessionSuggestions(sessionId: number, lastOutputType?: string): Promise<any[]> {
  const params = lastOutputType ? `?last_output_type=${lastOutputType}` : '';
  const { data } = await api.get(`/api/command-center/sessions/${sessionId}/suggestions${params}`);
  return data;
}

export async function triggerPipelineStep(sessionId: number, step: string): Promise<any> {
  const { data } = await api.post(`/api/command-center/sessions/${sessionId}/pipeline/${step}`);
  return data;
}

export async function linkTenderToSession(sessionId: number, tenderId: number): Promise<any> {
  const { data } = await api.patch(`/api/command-center/sessions/${sessionId}`, { tender_id: tenderId });
  return data;
}

export async function submitCommandCenterReview(sessionId: number): Promise<any> {
  const { data } = await api.post(`/api/command-center/sessions/${sessionId}/submit-review`);
  return data;
}

export async function getCommandCenterPipelineStatus(sessionId: number): Promise<any> {
  const { data } = await api.get(`/api/command-center/sessions/${sessionId}/pipeline/status`);
  return data;
}

export async function initSessionWorkspace(sessionId: number): Promise<any> {
  const { data } = await api.post(`/api/command-center/sessions/${sessionId}/init-workspace`);
  return data;
}

// ── Clarifications (Phase B) ───────────────────────────────────────────────

export interface PendingClarification {
  id: number;
  agent_key: string;
  question: string;
  options: string[];
  context: Record<string, any>;
  status: 'pending' | 'answered' | 'cancelled';
  answer: string | null;
  created_at: string | null;
  answered_at: string | null;
}

export async function listClarifications(
  sessionId: number,
  status?: 'pending' | 'answered' | 'cancelled',
): Promise<PendingClarification[]> {
  const params = status ? `?status=${status}` : '';
  const { data } = await api.get(
    `/api/command-center/sessions/${sessionId}/clarifications${params}`,
  );
  return data;
}

export async function cancelClarification(
  sessionId: number,
  clarificationId: number,
): Promise<{ id: number; status: string }> {
  const { data } = await api.post(
    `/api/command-center/sessions/${sessionId}/clarifications/${clarificationId}/cancel`,
  );
  return data;
}

/** POST an answer to a clarification. Returns the raw fetch Response so the
 *  caller can stream the resumed SSE events (the endpoint returns
 *  text/event-stream). */
export async function answerClarificationStream(
  sessionId: number,
  clarificationId: number,
  answer: string,
  signal?: AbortSignal,
): Promise<Response> {
  const token = localStorage.getItem('drpl_token');
  return fetch(
    `${API_BASE}/api/command-center/sessions/${sessionId}/clarifications/${clarificationId}/answer`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ answer }),
      signal,
    },
  );
}

// ── Agent Runs (durable, browser-close-survivable) ──────────────────────────
//
// New Phase C flow: enqueue a run on the backend, then stream its events
// out of Redis Streams. Falls back to the legacy inline /chat/stream when
// Redis is unavailable (503) so chats keep working either way.

export interface EnqueueRunBody {
  message: string;
  display_message?: string;
  proposal_session_id?: number;
  tender_id?: number;
  file_ids?: number[];
  file_metadata?: Record<string, any>;
  selected_agents?: string[];
}

export interface EnqueueRunResult {
  run_id: string;
  rq_job_id: string;
  status: string;
  events_url: string;
}

/** Queue a run on the backend. Throws on non-2xx. */
export async function enqueueRun(body: EnqueueRunBody): Promise<EnqueueRunResult> {
  const token = localStorage.getItem('drpl_token');
  const res = await fetch(`${API_BASE}/api/runs/enqueue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    const err: any = new Error(`enqueueRun ${res.status}: ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/** Open the SSE stream for a run. Resumes from last_event_id when given.
 *  Returns the raw Response so the caller can reuse the existing parser. */
export async function openRunStream(
  runId: string,
  lastEventId?: string,
  signal?: AbortSignal,
): Promise<Response> {
  const token = localStorage.getItem('drpl_token');
  const qs = lastEventId ? `?last_id=${encodeURIComponent(lastEventId)}` : '';
  return fetch(`${API_BASE}/api/runs/${runId}/events${qs}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
}

/** The run still in flight for this session, if the backend knows of one.
 *
 *  The localStorage pointer only exists in the browser that started the run.
 *  This is how the session finds its own work from a second device, another
 *  browser, or after site data was cleared — the run was always durable, it
 *  just had no address anyone else could reach it at.
 *
 *  Never throws: no run in flight is the ordinary answer, and a failure to
 *  ask must not stop the session from loading.
 */
export async function getActiveRun(sessionId: number): Promise<string | null> {
  try {
    const { data } = await api.get('/api/runs/active', {
      params: { proposal_session_id: sessionId },
    });
    return data?.run?.id ?? null;
  } catch {
    return null;
  }
}

/** Hit the legacy inline /chat/stream endpoint. Used when Redis is down. */
async function fetchLegacyChatStream(
  sessionId: number,
  body: { message: string; file_ids?: number[] },
  signal?: AbortSignal,
): Promise<Response> {
  const token = localStorage.getItem('drpl_token');
  return fetch(`${API_BASE}/api/command-center/sessions/${sessionId}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
    signal,
  });
}

/** POST a user's response to a decision_maker pending plan (approve / change /
 *  deny) and return the resulting SSE stream. The stream emits the same event
 *  types as the main chat/stream so the caller can reuse its parser. */
export async function respondToDecisionPlan(
  sessionId: number,
  action: 'approve' | 'change' | 'deny' | 'next',
  feedback?: string,
  signal?: AbortSignal,
): Promise<Response> {
  const token = localStorage.getItem('drpl_token');
  return fetch(`${API_BASE}/api/command-center/sessions/${sessionId}/decision/respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ action, feedback: feedback || '' }),
    signal,
  });
}

export interface StreamCommandCenterResult {
  response: Response;        // SSE stream — caller parses event:/data: pairs
  runId: string | null;      // null when we fell back to the legacy path
}

/** Unified command-center stream entry point.
 *
 *  1. Try enqueue → open /api/runs/{id}/events.
 *  2. On 503 (Redis down) fall back to the legacy /chat/stream.
 *
 *  The caller feeds `response.body` into the existing SSE parser; unknown
 *  event types (run_started / run_done) are silently ignored by the
 *  switch/case in CommandCenterPage.
 */
export async function streamCommandCenter(
  body: EnqueueRunBody & { file_ids?: number[] },
  signal?: AbortSignal,
): Promise<StreamCommandCenterResult> {
  if (!body.proposal_session_id) {
    throw new Error('streamCommandCenter requires proposal_session_id');
  }
  try {
    const { run_id } = await enqueueRun(body);
    const response = await openRunStream(run_id, undefined, signal);
    return { response, runId: run_id };
  } catch (err: any) {
    if (err?.status === 503) {
      const response = await fetchLegacyChatStream(
        body.proposal_session_id,
        { message: body.message, file_ids: body.file_ids },
        signal,
      );
      return { response, runId: null };
    }
    throw err;
  }
}

/** Stop a run that is queued or already executing.
 *
 * The Stop button used to abort this browser's fetch and nothing else — the
 * browser stopped listening while the worker ran to completion, still calling
 * models and still billing the user's monthly cap for work they had told it to
 * stop. Aborting the stream is now what happens *after* this resolves.
 *
 * `outcome` distinguishes the two live cases honestly: `cancelled` means
 * nothing had started and no model call was ever spent; `cancelling` means a
 * worker is executing it and will stop at its next event boundary.
 */
export async function cancelRun(runId: string): Promise<{
  run_id: string;
  status: string;
  outcome: 'cancelled' | 'cancelling' | 'already_finished';
}> {
  // The shared axios instance sets no timeout, and this call gates the Stop
  // button: if it hangs, the button spins and the stream keeps streaming. A
  // bounded wait, then the caller aborts locally regardless.
  const { data } = await api.post(`/api/runs/${runId}/cancel`, undefined, {
    timeout: 8_000,
  });
  return data;
}

// localStorage helpers so an in-flight run survives a page reload.
// Key per session so concurrent sessions don't clobber one another.
const RUN_STATE_KEY = (sessionId: number) => `drpl_active_run:${sessionId}`;

export function saveActiveRun(sessionId: number, runId: string, lastEventId?: string): void {
  try {
    localStorage.setItem(
      RUN_STATE_KEY(sessionId),
      JSON.stringify({ run_id: runId, last_event_id: lastEventId || '', ts: Date.now() }),
    );
  } catch { /* quota full — ignore */ }
}

export function loadActiveRun(sessionId: number): { run_id: string; last_event_id: string } | null {
  try {
    const raw = localStorage.getItem(RUN_STATE_KEY(sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    // Drop stale entries older than the Redis Stream TTL (1h).
    if (Date.now() - (parsed.ts || 0) > 60 * 60 * 1000) {
      localStorage.removeItem(RUN_STATE_KEY(sessionId));
      return null;
    }
    return { run_id: parsed.run_id, last_event_id: parsed.last_event_id || '' };
  } catch {
    return null;
  }
}

export function clearActiveRun(sessionId: number): void {
  try { localStorage.removeItem(RUN_STATE_KEY(sessionId)); } catch {}
}


export async function uploadChatAttachments(sessionId: number, files: File[]): Promise<any[]> {
  const formData = new FormData();
  files.forEach((f) => formData.append('files', f));
  const { data } = await api.post(`/api/command-center/sessions/${sessionId}/attachments`, formData, {
    headers: { 'Content-Type': undefined as any },
  });
  return data;
}

// ── Session Documents (Tender Library) ──────────────────────────────────────

export async function getSessionDocuments(sessionId: number): Promise<any> {
  const { data } = await api.get(`/api/command-center/sessions/${sessionId}/documents`);
  return data;
}

export async function triggerDocumentDownload(sessionId: number, docId: number): Promise<any> {
  const { data } = await api.post(`/api/command-center/sessions/${sessionId}/documents/${docId}/download`);
  return data;
}

export async function uploadSessionDocument(sessionId: number, file: File, gemFileId?: string): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const params = gemFileId ? `?gem_file_id=${gemFileId}` : '';
  const { data } = await api.post(
    `/api/command-center/sessions/${sessionId}/documents/upload${params}`,
    formData,
    { headers: { 'Content-Type': undefined as any } },
  );
  return data;
}

export async function triggerBulkDownload(sessionId: number): Promise<any> {
  const { data } = await api.post(`/api/command-center/sessions/${sessionId}/documents/download-all`);
  return data;
}

// ── Workspace (Canvas) ──────────────────────────────────────────────────────

export async function initWorkspace(tenderId: number): Promise<any> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/init`);
  return data;
}

export async function getWorkspace(tenderId: number): Promise<any> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/`);
  return data;
}

export async function updateWorkspaceConfig(tenderId: number, config: Record<string, any>): Promise<any> {
  const { data } = await api.patch(`/api/tenders/${tenderId}/workspace/config`, config);
  return data;
}

export async function saveWorkspaceLayout(tenderId: number, layoutJson: Record<string, any>): Promise<any> {
  const { data } = await api.patch(`/api/tenders/${tenderId}/workspace/layout`, { layout_json: layoutJson });
  return data;
}

export async function getDocumentWorkspace(tenderId: number, itemId: number): Promise<any> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/items/${itemId}`);
  return data;
}

export async function updateDocumentWorkspace(tenderId: number, itemId: number, updates: Record<string, any>): Promise<any> {
  const { data } = await api.patch(`/api/tenders/${tenderId}/workspace/items/${itemId}`, updates);
  return data;
}

export async function saveDocumentContent(tenderId: number, itemId: number, contentHtml: string | null, contentMarkdown: string | null): Promise<any> {
  const { data } = await api.put(`/api/tenders/${tenderId}/workspace/items/${itemId}/content`, {
    content_html: contentHtml,
    content_markdown: contentMarkdown,
  });
  return data;
}

export async function finalizeWorkspaceDocument(tenderId: number, itemId: number): Promise<any> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/items/${itemId}/finalize`);
  return data;
}

/** Render a workspace document to PDF using its current draft, selected
 *  letterhead, and signatures. Returns the linked generated_document_id. Does
 *  not lock or finalize the workspace. */
export async function generateWorkspaceDocumentPdf(
  tenderId: number,
  itemId: number,
): Promise<{ status: string; generated_document_id: number; file_name: string | null; file_size: number | null }> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/items/${itemId}/generate-pdf`);
  return data;
}

/** Set the page orientation ("portrait" | "landscape") on a workspace document.
 *  Controls editor width and the exported PDF's page orientation. */
export async function setDocumentOrientation(
  tenderId: number,
  itemId: number,
  orientation: 'portrait' | 'landscape',
): Promise<{ item_id: number; page_orientation: string }> {
  const { data } = await api.patch(
    `/api/tenders/${tenderId}/workspace/items/${itemId}/orientation`,
    { orientation },
  );
  return data;
}

/** Re-run the annexure_finder agent for an annexure-derived document and
 *  overwrite its current draft with a fresh, layout-preserving extraction.
 *  Only works on documents in drafting / not_started status. */
export async function reExtractAnnexure(
  tenderId: number,
  itemId: number,
): Promise<any> {
  const { data } = await api.post(
    `/api/tenders/${tenderId}/workspace/items/${itemId}/re-extract`,
  );
  return data;
}

// ── Combined annexure export (all annexures → one file) ─────────────────────

/** Live combined PDF preview of every annexure for a tender, stacked in order
 *  with letterhead + signatures baked in. Mirrors previewWorkspaceDocument but
 *  spans all annexures. Returns the authoritative rendered page count. */
export async function previewCombinedAnnexures(
  tenderId: number,
): Promise<{ blob: Blob; pageCount: number | null }> {
  const response = await api.get<Blob>(
    `/api/tenders/${tenderId}/annexures/preview-combined.pdf`,
    { responseType: 'blob' },
  );
  const hdr = (response.headers as any)['x-pdf-page-count'];
  const parsed = hdr != null ? Number(hdr) : NaN;
  return {
    blob: response.data,
    pageCount: Number.isFinite(parsed) && parsed > 0 ? parsed : null,
  };
}

/** Download all annexures merged into a single PDF. */
export async function exportAnnexuresPdf(tenderId: number): Promise<Blob> {
  const { data } = await api.get<Blob>(
    `/api/tenders/${tenderId}/annexures/export.pdf`,
    { responseType: 'blob' },
  );
  return data;
}

/** Download all annexures merged into a single editable DOCX. */
export async function exportAnnexuresDocx(tenderId: number): Promise<Blob> {
  const { data } = await api.get<Blob>(
    `/api/tenders/${tenderId}/annexures/export.docx`,
    { responseType: 'blob' },
  );
  return data;
}

/** Download a ZIP bundling the combined annexures PDF + DOCX. */
export async function exportAnnexuresZip(tenderId: number): Promise<Blob> {
  const { data } = await api.get<Blob>(
    `/api/tenders/${tenderId}/annexures/export.zip`,
    { responseType: 'blob' },
  );
  return data;
}

/** Manually add a blank annexure to a tender (for forms not in the documents).
 *  Returns the new checklist_item_id so the caller can scroll/expand it. */
export async function createManualAnnexure(
  tenderId: number,
  title: string,
  identifier?: string,
): Promise<{ checklist_item_id: number; workspace_id: number; identifier: string; item_name: string }> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/annexures`, {
    title,
    identifier: identifier || null,
  });
  return data;
}

/** Delete a workspace item (annexure/document) and its paired workspace row. */
export async function deleteWorkspaceItem(tenderId: number, itemId: number): Promise<{ deleted: number }> {
  const { data } = await api.delete(`/api/tenders/${tenderId}/workspace/items/${itemId}`);
  return data;
}

// ── Annexure letterhead ─────────────────────────────────────────────────────

/** Read the tender-wide default letterhead applied to every annexure. */
export async function getAnnexureLetterhead(
  tenderId: number,
): Promise<{ tender_id: number; default_letterhead_id: number | null }> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/annexures/letterhead`);
  return data;
}

/** Set (or clear, with null) the tender-wide default annexure letterhead.
 *  Per-annexure overrides still take precedence over this. */
export async function setAnnexureLetterhead(
  tenderId: number,
  letterheadTemplateId: number | null,
): Promise<{ tender_id: number; default_letterhead_id: number | null }> {
  const { data } = await api.put(`/api/tenders/${tenderId}/workspace/annexures/letterhead`, {
    letterhead_template_id: letterheadTemplateId,
  });
  return data;
}

/** Override the letterhead for ONE annexure.
 *  - disabled=true          → never apply a letterhead to this annexure
 *  - templateId=<n>         → use that specific template
 *  - disabled=false, null   → inherit the tender default */
export async function setItemLetterhead(
  tenderId: number,
  itemId: number,
  opts: { letterheadTemplateId?: number | null; disabled?: boolean },
): Promise<{ item_id: number; letterhead_template_id: number | null; letterhead_disabled: boolean }> {
  const { data } = await api.patch(
    `/api/tenders/${tenderId}/workspace/items/${itemId}/letterhead`,
    {
      letterhead_template_id: opts.letterheadTemplateId ?? null,
      disabled: opts.disabled ?? false,
    },
  );
  return data;
}

export async function toggleDocumentNotRequired(tenderId: number, itemId: number): Promise<any> {
  const { data } = await api.patch(`/api/tenders/${tenderId}/workspace/items/${itemId}/not-required`);
  return data;
}

export async function getFormatTemplates(tenderId: number): Promise<any[]> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/format-templates`);
  return data;
}

export async function getFormatTemplateById(tenderId: number, templateId: number): Promise<any> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/format-templates/${templateId}`);
  return data;
}

export async function createFormatTemplate(tenderId: number, template: Record<string, any>): Promise<any> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/format-templates`, template);
  return data;
}

export async function updateFormatTemplate(tenderId: number, templateId: number, updates: Record<string, any>): Promise<any> {
  const { data } = await api.patch(`/api/tenders/${tenderId}/workspace/format-templates/${templateId}`, updates);
  return data;
}

export async function getDocumentDependencies(tenderId: number, itemId: number): Promise<any> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/items/${itemId}/dependencies`);
  return data;
}

// Agent endpoints
export async function getDocumentAgentHistory(tenderId: number, itemId: number): Promise<any[]> {
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/items/${itemId}/agent/history`);
  return data;
}

export async function generateDocumentWithAgent(tenderId: number, itemId: number): Promise<any> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/items/${itemId}/agent/generate`);
  return data;
}

export async function enhanceDocumentWithAgent(tenderId: number, itemId: number, prompt: string): Promise<any> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/items/${itemId}/agent/enhance`, { prompt });
  return data;
}

export async function getAvailableAgents(tenderId: number, category?: string): Promise<any[]> {
  const params = category ? { category } : {};
  const { data } = await api.get(`/api/tenders/${tenderId}/workspace/agents/available`, { params });
  return data;
}

export async function autoAssignAgents(tenderId: number): Promise<any> {
  const { data } = await api.post(`/api/tenders/${tenderId}/workspace/agents/auto-assign`);
  return data;
}

// ── Workflow Builder ────────────────────────────────────────────────────────

export async function listWorkflows(params?: { status?: string; category?: string }): Promise<any[]> {
  const { data } = await api.get('/api/workflows', { params });
  return data;
}

export async function createWorkflow(body: { display_name: string; description?: string; category?: string; trigger_type?: string }): Promise<any> {
  const { data } = await api.post('/api/workflows', body);
  return data;
}

export async function getWorkflowById(workflowId: number): Promise<any> {
  const { data } = await api.get(`/api/workflows/${workflowId}`);
  return data;
}

export async function updateWorkflowById(workflowId: number, body: Record<string, any>): Promise<any> {
  const { data } = await api.put(`/api/workflows/${workflowId}`, body);
  return data;
}

export async function deleteWorkflow(workflowId: number): Promise<any> {
  const { data } = await api.delete(`/api/workflows/${workflowId}`);
  return data;
}

export async function saveWorkflowGraphApi(workflowId: number, body: { nodes: any[]; edges: any[]; canvas_state?: any }): Promise<any> {
  const { data } = await api.post(`/api/workflows/${workflowId}/graph`, body);
  return data;
}

export async function getWorkflowGraph(workflowId: number): Promise<any> {
  const { data } = await api.get(`/api/workflows/${workflowId}/graph`);
  return data;
}

export async function publishWorkflow(workflowId: number, changeDescription?: string): Promise<any> {
  const { data } = await api.post(`/api/workflows/${workflowId}/publish`, { change_description: changeDescription });
  return data;
}

export async function getWorkflowVersions(workflowId: number): Promise<any[]> {
  const { data } = await api.get(`/api/workflows/${workflowId}/versions`);
  return data;
}

export async function rollbackWorkflow(workflowId: number, versionNumber: number): Promise<any> {
  const { data } = await api.post(`/api/workflows/${workflowId}/rollback/${versionNumber}`);
  return data;
}

export async function cloneWorkflow(workflowId: number, displayName?: string): Promise<any> {
  const { data } = await api.post(`/api/workflows/${workflowId}/clone`, { display_name: displayName });
  return data;
}

export async function validateWorkflow(workflowId: number): Promise<any> {
  const { data } = await api.post(`/api/workflows/${workflowId}/validate`);
  return data;
}

export async function getWorkflowExecutions(workflowId: number, limit?: number): Promise<any[]> {
  const { data } = await api.get(`/api/workflows/${workflowId}/executions`, { params: { limit } });
  return data;
}

export async function getWorkflowExecution(executionId: number): Promise<any> {
  const { data } = await api.get(`/api/workflows/executions/${executionId}`);
  return data;
}

export function getWorkflowTestStreamUrl(workflowId: number): string {
  return `${API_BASE}/api/workflows/${workflowId}/test`;
}

// ─── Cost Breakdown editor ────────────────────────────────────────────────
import type { CostBreakdown, CostBreakdownLine } from '../types/command-center';

export async function getCostBreakdown(tenderId: number): Promise<CostBreakdown> {
  const res = await api.get(`/api/tenders/${tenderId}/cost-breakdown`);
  return res.data;
}

export async function updateCostBreakdown(
  tenderId: number,
  payload: {
    lines: CostBreakdownLine[];
    overhead_percent?: number;
    margin_percent?: number;
    gst_percent?: number;
  },
): Promise<CostBreakdown> {
  const res = await api.put(`/api/tenders/${tenderId}/cost-breakdown`, payload);
  return res.data;
}

/** Save an artifact's binary file to the user's disk.
 *
 * There is one download path for the whole app, because there used to be two
 * and they had drifted: the Command Center asked for a presigned link while the
 * cost-breakdown editor always streamed the bytes through the backend, which
 * meant the same spreadsheet arrived quickly from one button and slowly from
 * the other.
 *
 * Order of preference, fastest first:
 *   1. `directUrl` — a link the caller already holds (the regenerate endpoint
 *      returns one for the workbook it just built), so no round trip at all.
 *   2. `download-url` — a presigned R2 link; the bytes go R2 -> browser and
 *      never enter the backend or the JS heap.
 *   3. `download` — stream through the backend. This is the local-dev backend,
 *      and the path that regenerates a workbook whose stored object is gone.
 */
export async function saveArtifactFile(
  artifactId: number,
  fallbackName: string,
  directUrl?: string | null,
): Promise<void> {
  const token = localStorage.getItem('drpl_token');
  const auth = { Authorization: `Bearer ${token}` };

  const saveAs = (href: string, name: string) => {
    const a = document.createElement('a');
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  if (directUrl) {
    saveAs(directUrl, fallbackName);
    return;
  }

  // A failure here is not fatal — fall through to streaming rather than
  // reporting a download error the slow path could still have satisfied.
  try {
    const meta = await fetch(
      `${API_BASE}/api/command-center/artifacts/${artifactId}/download-url`,
      { headers: auth },
    );
    if (meta.ok) {
      const { url, file_name } = await meta.json();
      if (url) {
        saveAs(url, file_name || fallbackName);
        return;
      }
    }
  } catch {
    // Presigned-link request failed (network, CORS); the streaming path below
    // can still deliver the file.
  }

  const res = await fetch(
    `${API_BASE}/api/command-center/artifacts/${artifactId}/download`,
    { headers: auth },
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const disposition = res.headers.get('content-disposition') || '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  const blobUrl = URL.createObjectURL(await res.blob());
  saveAs(blobUrl, match ? match[1] : fallbackName);
  // Revoke on a later tick. `a.click()` only *starts* the download; revoking
  // the object URL in the same synchronous block can pull the bytes out from
  // under a browser that has not read them yet, which shows up as an
  // intermittent empty or failed save on exactly the slow path this fallback
  // exists to serve.
  setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
}

export async function regenerateCostXlsx(
  tenderId: number,
  sessionId?: number,
  opts?: { singleSheet?: boolean },
): Promise<{
  artifact_id: number;
  file_name: string;
  title: string;
  version: number;
  /** Presigned link to the workbook this call just built. Null on the local
   *  storage backend, where there is nothing to presign. */
  download_url?: string | null;
}> {
  const res = await api.post(`/api/tenders/${tenderId}/cost-breakdown/regenerate-xlsx`, {
    session_id: sessionId ?? null,
    // null → backend uses the costing.nit_single_sheet default; true → force
    // the single-tab NIT mirror; false → one sheet per schedule.
    single_sheet: opts?.singleSheet ?? null,
  });
  return res.data;
}

export async function promoteTenderToTraining(
  tenderId: number,
  payload?: { name?: string; tags?: string[] },
): Promise<{
  dataset_id: number;
  dataset_name: string;
  file_id: number;
  file_name: string;
  line_count: number;
  assigned_to_costing_researcher: boolean;
  assign_error: string | null;
}> {
  const res = await api.post(`/api/tenders/${tenderId}/promote-to-training`, payload ?? {});
  return res.data;
}

export async function regenerateCostPdf(
  tenderId: number,
  sessionId?: number,
): Promise<{ artifact_id: number; file_name: string; title: string; version: number }> {
  const res = await api.post(`/api/tenders/${tenderId}/cost-breakdown/regenerate-pdf`, {
    session_id: sessionId ?? null,
  });
  return res.data;
}

export interface TenderScheduleRow {
  id: number;
  sr_no: number;
  item_code: string | null;
  description: string;
  quantity: number | null;
  unit: string | null;
  estimated_rate: number | null;
  basic_value: number | null;
  escalation_pct: number | null;
  bidding_unit: string | null;
  schedule_name: string | null;
  is_tax_line: boolean;
}

export async function getTenderBiddingSchedule(
  tenderId: number,
): Promise<{ rows: TenderScheduleRow[] }> {
  const res = await api.get(`/api/tenders/${tenderId}/bidding-schedule`);
  return res.data;
}

export default api;

// --- Agent Builder: model catalog ---
// The backend owns the model list, provider list, per-model output ceilings and
// capability flags. The Agent Builder used to hardcode its own copies; they
// drifted silently, so an agent saved on one model displayed as another.
export async function getModelCatalog(): Promise<any> {
  const { data } = await api.get('/api/agent-builder/models');
  return data;
}

export async function getAgentEffectiveTools(agentId: number): Promise<any> {
  const { data } = await api.get(`/api/agent-builder/agents/${agentId}/effective-tools`);
  return data;
}

// --- GeM Search (server-side collector) ---
//
// A collect run IS an AgentRun: openRunStream / cancelRun / the reattach path
// all work on its id unchanged. These helpers only start, find and describe it.

export type CollectMode = 'incremental' | 'ministry' | 'full';

export interface StartCollectBody {
  portals?: string[];
  mode?: CollectMode;
  terms?: string[];
  ministries?: string[];
  max_pages?: number;
}

/**
 * What a finished sweep is entitled to claim, off the `portal_done` event.
 *
 * `coverage` and `complete` are present only when the sweep enumerated
 * something it can measure itself against -- `full` against the whole portal,
 * `ministry` against GeM's own count for that ministry. An `incremental` sweep
 * stops on known ground by design and omits them: absent means "not measured",
 * never "complete", and the page must not fill in a reassuring default.
 */
export interface CollectOutcome {
  portal: string;
  mode: string;
  status?: string;
  coverage?: number;
  complete?: boolean;
  rows_distinct?: number;
  pages_failed?: number;
  final_total?: number | null;
}

export interface StartCollectResult {
  run_id: string;
  rq_job_id: string;
  status: string;
  events_url: string;
  params: Record<string, unknown>;
}

export async function startCollectRun(body: StartCollectBody = {}): Promise<StartCollectResult> {
  const token = localStorage.getItem('drpl_token');
  const res = await fetch(`${API_BASE}/api/collect/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    const err: any = new Error(`startCollectRun ${res.status}: ${detail}`);
    err.status = res.status;
    // 409 carries the id of the sweep already running, so the page can watch
    // that one instead of reporting a dead end.
    if (res.status === 409) {
      try { err.existingRunId = JSON.parse(detail)?.detail?.run_id; } catch { /* noop */ }
    }
    throw err;
  }
  return res.json();
}

export interface ActiveCollectRun {
  id: string;
  status: string;
  created_at: string | null;
  params: Record<string, unknown>;
  mine: boolean;
}

/** The sweep still in flight, if the backend knows of one. Never throws. */
export async function getActiveCollectRun(): Promise<ActiveCollectRun | null> {
  try {
    const { data } = await api.get('/api/collect/active');
    return data?.run ?? null;
  } catch {
    return null;
  }
}

export interface CollectCoverage {
  available: boolean;
  portal: string;
  tenders_seen?: number;
  open_gaps?: number;
  last_sweep?: {
    run_id: string; started_at: string | null; finished_at: string | null; status: string;
    rows_new: number; rows_seen: number; expected_total: number | null; mode: string;
  } | null;
  drift?: Array<{ field: string; fill_rate: number; baseline: number; detected_at?: string }>;
}

export async function getCollectCoverage(portal = 'gem'): Promise<CollectCoverage> {
  const { data } = await api.get('/api/collect/coverage', { params: { portal } });
  return data;
}

export interface RecentCollectRun {
  id: string;
  status: string;
  created_at: string | null;
  finished_at: string | null;
  params: Record<string, unknown>;
  summary: string | null;
  error: string | null;
  mine: boolean;
}

export async function getRecentCollectRuns(limit = 8): Promise<RecentCollectRun[]> {
  const { data } = await api.get('/api/collect/recent', { params: { limit } });
  return data?.runs ?? [];
}
