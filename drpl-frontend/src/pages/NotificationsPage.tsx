import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, CheckCheck, Filter, Inbox } from 'lucide-react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import NotificationItem from '../components/notifications/NotificationItem';
import {
  listNotifications,
  markNotificationRead,
  markAllNotificationsRead,
} from '../lib/notifications';
import type { AppNotification } from '../types/notification';

const PAGE_SIZE = 50;

export default function NotificationsPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<AppNotification[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);

  async function load(o: number, ufilter: boolean) {
    setLoading(true);
    try {
      const res = await listNotifications({
        limit: PAGE_SIZE,
        offset: o,
        unread_only: ufilter,
      });
      setItems(res.items);
      setTotal(res.total);
      setOffset(o);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(0, unreadOnly);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unreadOnly]);

  const handleItemClick = async (n: AppNotification) => {
    if (!n.is_read) {
      try {
        await markNotificationRead(n.id);
        setItems((prev) =>
          prev.map((x) => (x.id === n.id ? { ...x, is_read: true } : x)),
        );
      } catch {
        // ignore
      }
    }
    if (!n.action_url) return;
    if (/^https?:\/\//i.test(n.action_url)) {
      window.location.href = n.action_url;
    } else {
      navigate(n.action_url);
    }
  };

  const handleMarkAllRead = async () => {
    await markAllNotificationsRead();
    setItems((prev) => prev.map((x) => ({ ...x, is_read: true })));
  };

  const hasNext = offset + PAGE_SIZE < total;
  const hasPrev = offset > 0;

  return (
    <>
      <Header title="Notifications" subtitle="Updates that may need your attention" />
      <div className="mx-auto w-full max-w-4xl p-4 sm:p-6 lg:p-8">
        <div className="flex items-center justify-between mb-4">
          <button
            type="button"
            onClick={() => setUnreadOnly((v) => !v)}
            className={`flex items-center gap-2 px-3 py-1.5 text-sm rounded-lg border transition-colors ${
              unreadOnly
                ? 'border-accent/40 bg-accent/10 text-accent'
                : 'border-border bg-card text-muted-foreground hover:bg-muted/40'
            }`}
          >
            <Filter size={14} />
            {unreadOnly ? 'Showing unread only' : 'Show all'}
          </button>
          <button
            type="button"
            onClick={handleMarkAllRead}
            disabled={items.every((n) => n.is_read)}
            className="flex items-center gap-2 px-3 py-1.5 text-sm rounded-lg border border-border bg-card text-muted-foreground hover:bg-muted/40 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <CheckCheck size={14} />
            Mark all read
          </button>
        </div>

        <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-card">
          {loading ? (
            <div className="py-12"><LoadingSpinner /></div>
          ) : items.length === 0 ? (
            <div className="px-4 py-16 text-center">
              <Inbox size={36} className="mx-auto text-muted-foreground/50 mb-3" />
              <p className="font-semibold text-foreground">You are all caught up</p>
              <p className="mt-1 text-sm text-muted-foreground">New tender and document updates will appear here.</p>
              <button onClick={() => navigate('/tenders')} className="mt-5 inline-flex items-center gap-2 rounded-xl bg-foreground px-4 py-2.5 text-sm font-bold text-background">Browse tenders <ArrowRight size={14} /></button>
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {items.map((n) => (
                <li key={n.id}>
                  <NotificationItem notification={n} onClick={handleItemClick} />
                </li>
              ))}
            </ul>
          )}
        </div>

        {(hasPrev || hasNext) && (
          <div className="flex items-center justify-between mt-4 text-sm">
            <button
              type="button"
              onClick={() => load(Math.max(0, offset - PAGE_SIZE), unreadOnly)}
              disabled={!hasPrev}
              className="px-3 py-1.5 border border-border rounded-lg text-muted-foreground hover:bg-muted/40 disabled:opacity-40"
            >
              Previous
            </button>
            <span className="text-muted-foreground">
              {offset + 1}–{Math.min(offset + items.length, total)} of {total}
            </span>
            <button
              type="button"
              onClick={() => load(offset + PAGE_SIZE, unreadOnly)}
              disabled={!hasNext}
              className="px-3 py-1.5 border border-border rounded-lg text-muted-foreground hover:bg-muted/40 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </>
  );
}
