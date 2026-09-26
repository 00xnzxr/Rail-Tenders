import clsx from 'clsx';

interface StatusBadgeProps {
  status: string;
}

// Semantic status colors with dark-mode variants. Color carries meaning here
// (good UX), so we keep the hue per status but make both themes legible.
const statusStyles: Record<string, string> = {
  open: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400',
  closed: 'bg-muted text-muted-foreground',
  awarded: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-400',
  cancelled: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-400',
  running: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-400',
  completed: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400',
  error: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-400',
};

export default function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span
      className={clsx(
        'inline-block px-2.5 py-0.5 rounded-full text-xs font-medium capitalize',
        statusStyles[status] || 'bg-muted text-muted-foreground'
      )}
    >
      {status}
    </span>
  );
}
