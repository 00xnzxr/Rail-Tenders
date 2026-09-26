const priorityConfig: Record<string, { label: string; className: string }> = {
  critical: { label: 'Critical', className: 'text-red-700 bg-red-100 dark:bg-red-500/15 dark:text-red-400' },
  high: { label: 'High', className: 'text-orange-700 bg-orange-100 dark:bg-orange-500/15 dark:text-orange-400' },
  medium: { label: 'Medium', className: 'text-blue-700 bg-blue-100 dark:bg-blue-500/15 dark:text-blue-400' },
  low: { label: 'Low', className: 'text-muted-foreground bg-muted' },
};

export default function PriorityBadge({ priority }: { priority: string }) {
  const config = priorityConfig[priority] || priorityConfig.medium;
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${config.className}`}>
      {config.label}
    </span>
  );
}
