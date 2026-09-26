// ============================================================
// DRPL Extension - API Client
// Handles all communication with the DRPL backend
// ============================================================

import { ApiResponse, BatchUploadResponse, TenderData } from './types';

const DEFAULT_BACKEND_URL = 'https://drpl-platform-production.up.railway.app';
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

// --- Helper: Get stored auth config ---

async function getConfig(): Promise<{ backendUrl: string; authToken: string | null }> {
  const result = await chrome.storage.local.get(['backendUrl', 'authToken']);
  let backendUrl = result.backendUrl || DEFAULT_BACKEND_URL;

  // Auto-correct stale dev URLs persisted from earlier installs
  if (
    backendUrl.includes('localhost') ||
    backendUrl.includes('127.0.0.1') ||
    backendUrl.includes('drplai.vercel.app') // frontend URL accidentally set as backend
  ) {
    backendUrl = DEFAULT_BACKEND_URL;
    await chrome.storage.local.set({ backendUrl });
  }

  return { backendUrl, authToken: result.authToken || null };
}

// --- Helper: Make authenticated request with retry ---

async function apiRequest<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<ApiResponse<T>> {
  const { backendUrl, authToken } = await getConfig();

  if (!authToken) {
    return { success: false, error: 'Not authenticated. Please sign in from the popup.' };
  }

  const url = `${backendUrl}/api/extension${endpoint}`;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${authToken}`,
    ...((options.headers as Record<string, string>) || {}),
  };

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = await fetch(url, { ...options, headers });

      if (response.status === 401) {
        // Clear stored auth so popup reacts
        await chrome.storage.local.remove(['authToken', 'userId', 'userEmail']);
        return { success: false, error: 'Session expired. Please log in again.' };
      }

      if (!response.ok) {
        const errorBody = await response.text();
        if (attempt < MAX_RETRIES) {
          await sleep(RETRY_DELAY_MS * attempt);
          continue;
        }
        return { success: false, error: `Server error ${response.status}: ${errorBody}` };
      }

      const data = await response.json();
      return { success: true, data };
    } catch (err) {
      if (attempt < MAX_RETRIES) {
        await sleep(RETRY_DELAY_MS * attempt);
        continue;
      }
      return { success: false, error: `Network error: ${(err as Error).message}` };
    }
  }

  return { success: false, error: 'Max retries exceeded' };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- Public API Methods ---

/** Upload a batch of extracted tenders to the backend */
export async function uploadTenders(tenders: TenderData[]): Promise<ApiResponse<BatchUploadResponse>> {
  return apiRequest<BatchUploadResponse>('/tenders', {
    method: 'POST',
    body: JSON.stringify({ tenders }),
  });
}

/** Report scraping session status */
export async function reportStatus(status: {
  portal: string;
  sessionId: string;
  status: string;
  tendersFound: number;
  error?: string;
}): Promise<ApiResponse> {
  return apiRequest('/status', {
    method: 'POST',
    body: JSON.stringify(status),
  });
}

/** Fetch the latest selector configuration from backend */
export async function fetchSelectors(portal: string): Promise<ApiResponse<any>> {
  return apiRequest(`/selectors/${portal}`, { method: 'GET' });
}

/** Fetch extension config (feature flags, sync interval, scope profile) */
export async function fetchConfig(): Promise<ApiResponse<any>> {
  return apiRequest('/config', { method: 'GET' });
}

/**
 * Phase 7 — fetch the latest config from the backend, persist the
 * scope_profile block to chrome.storage.local so the GeM auto-search
 * driver and the popup KeywordPanel can read it without an extra round
 * trip. Returns the cached profile or null.
 */
export async function syncScopeProfile(): Promise<any | null> {
  const result = await fetchConfig();
  if (!result.success) return null;
  const profile = result.data?.scope_profile || null;
  const features = result.data?.features || {};
  await chrome.storage.local.set({
    scopeProfile: profile,
    extensionFeatures: features,
    scopeProfileSyncedAt: new Date().toISOString(),
  });
  return profile;
}

// --- Authentication ---

/** Authenticate with the DRPL backend and store the JWT */
export async function loginToBackend(
  email: string,
  password: string,
  backendUrl?: string
): Promise<ApiResponse<{ access_token: string; user_id: number; email: string }>> {
  const url = backendUrl || (await getConfig()).backendUrl;

  try {
    const response = await fetch(`${url}/api/auth/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });

    if (response.status === 401) {
      return { success: false, error: 'Invalid email or password.' };
    }
    if (response.status === 403) {
      return { success: false, error: 'Account is disabled. Contact your administrator.' };
    }
    if (!response.ok) {
      return { success: false, error: `Server error: ${response.status}` };
    }

    const data = await response.json();

    await chrome.storage.local.set({
      backendUrl: url.replace(/\/$/, ''),
      authToken: data.access_token,
      userId: String(data.user_id),
      userEmail: data.email,
    });

    return { success: true, data };
  } catch {
    return { success: false, error: 'Cannot connect to backend. Check the URL.' };
  }
}

/** Clear stored auth data (logout) */
export async function logoutFromBackend(): Promise<void> {
  await chrome.storage.local.remove(['authToken', 'userId', 'userEmail']);
}

/** Upload a tender document (PDF).
 *
 * `tenderId` may be a portal-native id (e.g., "L9265359A"); pass `portal`
 * alongside so the backend can resolve to the right Tender row. Without
 * `portal`, a non-numeric `tenderId` silently bound to tender 0 — kept for
 * backward compat callers that already pass DB ids.
 */
export async function uploadDocument(
  tenderId: string,
  fileName: string,
  fileBlob: Blob,
  documentType?: string,
  portal?: string,
  sourceUrl?: string,
): Promise<ApiResponse> {
  const { backendUrl, authToken } = await getConfig();
  if (!authToken) {
    return { success: false, error: 'Not authenticated.' };
  }

  const formData = new FormData();
  formData.append('tender_id', tenderId);
  formData.append('file', fileBlob, fileName);
  if (documentType) {
    formData.append('document_type', documentType);
  }
  if (portal) {
    formData.append('portal', portal);
  }
  if (sourceUrl) {
    formData.append('source_url', sourceUrl);
  }

  try {
    const response = await fetch(`${backendUrl}/api/extension/documents`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${authToken}` },
      body: formData,
    });

    if (!response.ok) {
      return { success: false, error: `Upload failed: ${response.status}` };
    }

    const data = await response.json();
    return { success: true, data };
  } catch (err) {
    return { success: false, error: `Upload error: ${(err as Error).message}` };
  }
}
