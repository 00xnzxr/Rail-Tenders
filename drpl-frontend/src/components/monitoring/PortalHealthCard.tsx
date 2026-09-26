import type { PortalHealthInfo } from '../../types/monitoring';
import { portalLabel, formatTimeAgo } from '../../lib/formatters';
import clsx from 'clsx';

interface PortalHealthCardProps {
  health: PortalHealthInfo;
}

const statusDot: Record<string, string> = {
  healthy: 'bg-emerald-500',
  warning: 'bg-yellow-500',
  error: 'bg-red-500',
};

const statusLabel: Record<string, string> = {
  healthy: 'Healthy',
  warning: 'Warning',
  error: 'Error',
};

export default function PortalHealthCard({ health }: PortalHealthCardProps) {
  return (
    <div className="bg-card rounded-lg border border-border p-4">
      <div className="flex items-center justify-between mb-3">
        <h4 className="font-medium text-foreground">{portalLabel(health.portal)}</h4>
        <div className="flex items-center gap-1.5">
          <div className={clsx('w-2.5 h-2.5 rounded-full', statusDot[health.status])} />
          <span className={clsx(
            'text-xs font-medium',
            health.status === 'healthy' && 'text-emerald-600 dark:text-emerald-400',
            health.status === 'warning' && 'text-yellow-600 dark:text-yellow-400',
            health.status === 'error' && 'text-red-600 dark:text-red-400',
          )}>
            {statusLabel[health.status]}
          </span>
        </div>
      </div>

      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-muted-foreground">Last Scrape</span>
          <span className="text-muted-foreground">
            {health.last_successful_scrape ? formatTimeAgo(health.last_successful_scrape) : 'Never'}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">24h Tenders</span>
          <span className="text-foreground font-medium">{health.tenders_24h}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">Success Rate</span>
          <span className={clsx(
            'font-medium',
            health.success_rate >= 0.8 ? 'text-emerald-600 dark:text-emerald-400' : health.success_rate >= 0.5 ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-600 dark:text-red-400',
          )}>
            {Math.round(health.success_rate * 100)}%
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted-foreground">Selectors</span>
          <span className="text-muted-foreground font-mono text-xs">v{health.selectors_version}</span>
        </div>
      </div>

      {health.last_error && (
        <div className="mt-3 px-2 py-1.5 bg-red-50 dark:bg-red-500/15 border border-red-100 rounded text-xs text-red-600 dark:text-red-400 truncate">
          {health.last_error}
        </div>
      )}
    </div>
  );
}
