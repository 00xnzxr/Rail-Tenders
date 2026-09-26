import clsx from 'clsx';
import { portalLabel } from '../../lib/formatters';

interface PortalBadgeProps {
  portal: string;
}

const portalStyles: Record<string, string> = {
  ireps: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-400',
  gem: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400',
  tendertiger: 'bg-orange-50 text-orange-700 dark:bg-orange-500/15 dark:text-orange-400',
  bidassist: 'bg-purple-50 text-purple-700 dark:bg-purple-500/15 dark:text-purple-400',
};

export default function PortalBadge({ portal }: PortalBadgeProps) {
  return (
    <span
      className={clsx(
        'inline-block px-2.5 py-0.5 rounded-full text-xs font-medium',
        portalStyles[portal] || 'bg-muted text-muted-foreground'
      )}
    >
      {portalLabel(portal)}
    </span>
  );
}
