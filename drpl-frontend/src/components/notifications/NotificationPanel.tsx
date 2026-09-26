import { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import { Inbox, CheckCheck } from 'lucide-react';
import NotificationItem from './NotificationItem';
import type { AppNotification } from '../../types/notification';

interface Props {
  items: AppNotification[];
  unread: number;
  onClose: () => void;
  onItemClick: (n: AppNotification) => void;
  onMarkAllRead: () => void;
}

export default function NotificationPanel({
  items, unread, onClose, onItemClick, onMarkAllRead,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  // Close on click outside.
  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [onClose]);

  // Close on Escape.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const recent = items.slice(0, 10);

  return (
    <div
      ref={ref}
      className="absolute right-0 top-full mt-2 w-96 max-w-[calc(100vw-32px)] bg-card border border-border rounded-lg shadow-lg z-50 overflow-hidden"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-foreground">Notifications</h3>
          {unread > 0 && (
            <span className="text-xs bg-accent/15 text-accent rounded-full px-2 py-0.5">
              {unread} new
            </span>
          )}
        </div>
        {unread > 0 && (
          <button
            type="button"
            onClick={onMarkAllRead}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
          >
            <CheckCheck size={13} />
            Mark all read
          </button>
        )}
      </div>

      <div className="max-h-[480px] overflow-y-auto">
        {recent.length === 0 ? (
          <div className="px-4 py-12 text-center">
            <Inbox size={28} className="mx-auto text-muted-foreground/50 mb-2" />
            <p className="text-sm text-muted-foreground">No notifications yet</p>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {recent.map((n) => (
              <li key={n.id}>
                <NotificationItem
                  notification={n}
                  onClick={(ev) => {
                    onItemClick(ev);
                    onClose();
                  }}
                  compact
                />
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="border-t border-border px-4 py-2.5">
        <Link
          to="/notifications"
          onClick={onClose}
          className="text-xs text-accent hover:underline"
        >
          View all notifications &rarr;
        </Link>
      </div>
    </div>
  );
}
