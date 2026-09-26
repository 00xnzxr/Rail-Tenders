import { useEffect, useRef, useState, useCallback } from 'react';
import {
  listNotifications, getUnreadCount, openNotificationsSSE,
  markNotificationRead, markAllNotificationsRead,
} from '../lib/notifications';
import type { AppNotification } from '../types/notification';

/**
 * Notification feed hook.
 *
 * - Hydrates an initial page of notifications + unread count via REST.
 * - Opens an SSE stream and prepends new notifications as they arrive.
 * - Falls back to polling every 60s if the SSE connection errors out.
 */
export function useNotificationsSSE(opts: { initialLimit?: number } = {}) {
  const { initialLimit = 20 } = opts;
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [hydrated, setHydrated] = useState(false);

  const sseRef = useRef<EventSource | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const hydrate = useCallback(async () => {
    try {
      const [list, c] = await Promise.all([
        listNotifications({ limit: initialLimit }),
        getUnreadCount(),
      ]);
      setItems(list.items);
      setUnread(c);
      setHydrated(true);
    } catch {
      // ignore — bell will just show 0 until next attempt
    }
  }, [initialLimit]);

  const refreshUnread = useCallback(async () => {
    try {
      setUnread(await getUnreadCount());
    } catch {
      // swallow
    }
  }, []);

  // Mount: hydrate + open SSE.
  useEffect(() => {
    let cancelled = false;
    hydrate();

    const es = openNotificationsSSE({
      onNotification: (n) => {
        if (cancelled) return;
        setItems((prev) => [n, ...prev].slice(0, 100));
        if (!n.is_read) setUnread((u) => u + 1);
      },
      onError: () => {
        // SSE failure → fall back to polling every 60s. Don't tear down items.
        if (pollRef.current) return;
        pollRef.current = setInterval(() => {
          hydrate();
        }, 60_000);
      },
    });
    sseRef.current = es;

    return () => {
      cancelled = true;
      if (sseRef.current) {
        sseRef.current.close();
        sseRef.current = null;
      }
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [hydrate]);

  const markRead = useCallback(async (id: number) => {
    setItems((prev) => prev.map((n) => (n.id === id ? { ...n, is_read: true } : n)));
    setUnread((u) => Math.max(0, u - 1));
    try {
      await markNotificationRead(id);
    } catch {
      // sync state with server on failure
      refreshUnread();
    }
  }, [refreshUnread]);

  const markAllRead = useCallback(async () => {
    setItems((prev) => prev.map((n) => ({ ...n, is_read: true })));
    setUnread(0);
    try {
      await markAllNotificationsRead();
    } catch {
      refreshUnread();
    }
  }, [refreshUnread]);

  return { items, unread, hydrated, markRead, markAllRead, refresh: hydrate };
}
