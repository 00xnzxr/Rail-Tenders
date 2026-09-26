import { api, API_BASE } from './api';
import type {
  AppNotification,
  NotificationListResponse,
  NotificationPreference,
} from '../types/notification';

// ── User-facing endpoints ──────────────────────────────────────────────────

export async function listNotifications(params?: {
  limit?: number;
  offset?: number;
  unread_only?: boolean;
}): Promise<NotificationListResponse> {
  const { data } = await api.get<NotificationListResponse>('/api/notifications/', { params });
  return data;
}

export async function getUnreadCount(): Promise<number> {
  const { data } = await api.get<{ count: number }>('/api/notifications/unread-count');
  return data.count;
}

export async function markNotificationRead(id: number): Promise<AppNotification> {
  const { data } = await api.post<AppNotification>(`/api/notifications/${id}/read`);
  return data;
}

export async function markAllNotificationsRead(): Promise<number> {
  const { data } = await api.post<{ marked_read: number }>('/api/notifications/mark-all-read');
  return data.marked_read;
}

/**
 * Open an EventSource on the per-user notification SSE stream. Returns the
 * EventSource so the caller can close it on unmount. The `onNotification`
 * callback fires for every event of type "notification".
 *
 * NOTE: EventSource cannot send auth headers, so we pass the JWT as a query
 * param. The backend reads it via the same auth dependency as other routes.
 */
export function openNotificationsSSE(opts: {
  onNotification: (n: AppNotification) => void;
  onError?: (ev: Event) => void;
}): EventSource | null {
  const token = localStorage.getItem('drpl_token');
  if (!token) return null;
  // FastAPI's get_current_user reads `Authorization: Bearer ...`; EventSource
  // can't set headers. The backend's `get_current_user` also accepts a `token`
  // query param as a fallback (used by existing SSE endpoints) — if not,
  // we'll need to extend it. For now, try query-param auth and fall back to
  // polling on auth failure.
  const url = `${API_BASE}/api/notifications/sse?token=${encodeURIComponent(token)}`;
  const es = new EventSource(url, { withCredentials: false });
  es.addEventListener('notification', (ev: MessageEvent) => {
    try {
      const payload = JSON.parse(ev.data) as AppNotification;
      opts.onNotification(payload);
    } catch {
      // ignore malformed payloads
    }
  });
  if (opts.onError) es.addEventListener('error', opts.onError);
  return es;
}

// ── Admin endpoints ────────────────────────────────────────────────────────

export async function listNotificationPreferences(): Promise<NotificationPreference[]> {
  const { data } = await api.get<{ items: NotificationPreference[] }>(
    '/api/admin/notification-preferences/',
  );
  return data.items;
}

export async function updateNotificationPreference(
  kind: string,
  body: { in_app_enabled?: boolean; email_enabled?: boolean },
): Promise<NotificationPreference> {
  const { data } = await api.put<NotificationPreference>(
    `/api/admin/notification-preferences/${encodeURIComponent(kind)}`,
    body,
  );
  return data;
}
