import { useNavigate } from 'react-router-dom';
import {
  Bell, FileCheck, Clock, AlertTriangle, FileText, CheckCircle, XCircle,
  RefreshCw, BookOpen, DollarSign, Mail,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { AppNotification } from '../../types/notification';

interface Props {
  notification: AppNotification;
  onClick?: (n: AppNotification) => void;
  compact?: boolean;
}

const KIND_ICON: Record<string, LucideIcon> = {
  'tender.assigned': FileText,
  'tender.analysis_complete': FileCheck,
  'tender.costing_complete': DollarSign,
  'tender.annexures_extracted': BookOpen,
  'tender.closing_soon': Clock,
  'proposal.submitted': Mail,
  'proposal.approved': CheckCircle,
  'proposal.rejected': XCircle,
  'proposal.changes_requested': RefreshCw,
  'agent_run.failed': AlertTriangle,
  'digest.daily': Bell,
};

const KIND_COLOR: Record<string, string> = {
  'tender.assigned': 'text-accent bg-accent/10',
  'tender.analysis_complete': 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-500/15',
  'tender.costing_complete': 'text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/15',
  'tender.annexures_extracted': 'text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-500/15',
  'tender.closing_soon': 'text-rose-600 dark:text-rose-400 bg-rose-50 dark:bg-rose-500/15',
  'proposal.submitted': 'text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-500/15',
  'proposal.approved': 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-500/15',
  'proposal.rejected': 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15',
  'proposal.changes_requested': 'text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/15',
  'agent_run.failed': 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15',
  'digest.daily': 'text-muted-foreground bg-muted/40',
};

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(iso).toLocaleDateString();
}

export default function NotificationItem({ notification: n, onClick, compact }: Props) {
  const navigate = useNavigate();
  const Icon = KIND_ICON[n.kind] || Bell;
  const color = KIND_COLOR[n.kind] || 'text-muted-foreground bg-muted/40';

  const handleClick = () => {
    onClick?.(n);
    if (!n.action_url) return;
    // External (http/https) → full-page nav; relative path → SPA navigate.
    if (/^https?:\/\//i.test(n.action_url)) {
      window.location.href = n.action_url;
    } else {
      navigate(n.action_url);
    }
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      className={`w-full text-left flex items-start gap-3 px-3 py-3 transition-colors ${
        n.is_read ? 'hover:bg-muted/40' : 'bg-accent/5 hover:bg-accent/10'
      }`}
    >
      <div className={`flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center ${color}`}>
        <Icon size={16} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-2">
          <p className={`text-sm leading-snug ${n.is_read ? 'text-foreground' : 'font-medium text-foreground'}`}>
            {n.title}
          </p>
          {!n.is_read && (
            <span className="flex-shrink-0 w-2 h-2 rounded-full bg-accent/100 mt-1.5" aria-label="Unread" />
          )}
        </div>
        {!compact && n.body && (
          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{n.body}</p>
        )}
        <p className="text-[11px] text-muted-foreground mt-1">{timeAgo(n.created_at)}</p>
      </div>
    </button>
  );
}
