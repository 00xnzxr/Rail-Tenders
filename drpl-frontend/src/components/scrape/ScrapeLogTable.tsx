import type { ScrapeSession } from '../../types/scrape';
import StatusBadge from '../ui/StatusBadge';
import PortalBadge from '../ui/PortalBadge';
import EmptyState from '../ui/EmptyState';
import { formatDateTime, formatTimeAgo } from '../../lib/formatters';

interface ScrapeLogTableProps {
  logs: ScrapeSession[];
}

export default function ScrapeLogTable({ logs }: ScrapeLogTableProps) {
  if (logs.length === 0) {
    return <EmptyState message="No scrape sessions recorded yet" />;
  }

  return (
    <div className="bg-card rounded-lg border border-border overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="bg-muted/40 border-b border-border">
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Portal</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Session ID</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Status</th>
              <th className="px-4 py-3 text-right text-xs font-medium text-muted-foreground uppercase tracking-wider">Tenders Found</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Started</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Completed</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Error</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((log) => (
              <tr key={log.id} className="border-b border-border">
                <td className="px-4 py-3"><PortalBadge portal={log.portal} /></td>
                <td className="px-4 py-3 text-sm text-muted-foreground font-mono">{log.session_id || '-'}</td>
                <td className="px-4 py-3">{log.status ? <StatusBadge status={log.status} /> : '-'}</td>
                <td className="px-4 py-3 text-sm text-foreground text-right font-medium">{log.tenders_found}</td>
                <td className="px-4 py-3 text-sm text-muted-foreground" title={formatDateTime(log.started_at)}>
                  {formatTimeAgo(log.started_at)}
                </td>
                <td className="px-4 py-3 text-sm text-muted-foreground">
                  {log.completed_at ? formatTimeAgo(log.completed_at) : '-'}
                </td>
                <td className="px-4 py-3 text-sm text-red-600 dark:text-red-400 max-w-xs truncate">
                  {log.error_message || '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
