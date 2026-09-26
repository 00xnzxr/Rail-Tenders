import { useState } from 'react';
import { Bell } from 'lucide-react';
import NotificationPanel from './NotificationPanel';
import { useNotificationsSSE } from '../../hooks/useNotificationsSSE';

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const { items, unread, markRead, markAllRead } = useNotificationsSSE();

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="relative w-9 h-9 rounded-full hover:bg-muted flex items-center justify-center transition-colors"
        aria-label={`Notifications${unread > 0 ? ` (${unread} unread)` : ''}`}
      >
        <Bell size={17} strokeWidth={1.75} className="text-muted-foreground" />
        {unread > 0 && (
          <span className="absolute top-1 right-1 min-w-[16px] h-4 px-1 rounded-full bg-red-500 text-white text-[10px] font-bold flex items-center justify-center">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
      {open && (
        <NotificationPanel
          items={items}
          unread={unread}
          onClose={() => setOpen(false)}
          onItemClick={(n) => {
            if (!n.is_read) markRead(n.id);
          }}
          onMarkAllRead={markAllRead}
        />
      )}
    </div>
  );
}
