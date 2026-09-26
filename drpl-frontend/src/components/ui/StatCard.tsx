import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: LucideIcon;
  color?: 'blue' | 'green' | 'orange' | 'slate';
}

const colorMap = {
  blue: {
    text: 'text-blue-600 dark:text-blue-400',
    iconBg: 'bg-blue-50 dark:bg-blue-500/15',
    iconText: 'text-blue-500 dark:text-blue-400',
  },
  green: {
    text: 'text-emerald-600 dark:text-emerald-400',
    iconBg: 'bg-emerald-50 dark:bg-emerald-500/15',
    iconText: 'text-emerald-500 dark:text-emerald-400',
  },
  orange: {
    text: 'text-orange-600 dark:text-orange-400',
    iconBg: 'bg-orange-50 dark:bg-orange-500/15',
    iconText: 'text-orange-500 dark:text-orange-400',
  },
  slate: {
    text: 'text-foreground',
    iconBg: 'bg-muted',
    iconText: 'text-muted-foreground',
  },
};

export default function StatCard({ label, value, icon: Icon, color = 'blue' }: StatCardProps) {
  const c = colorMap[color];

  return (
    <div className="bg-card rounded-xl border border-border p-5 flex items-center gap-4 shadow-card hover:shadow-card-hover transition-shadow duration-200">
      {Icon && (
        <div className={clsx('p-2.5 rounded-lg shrink-0', c.iconBg, c.iconText)}>
          <Icon size={20} strokeWidth={1.75} />
        </div>
      )}
      <div className="min-w-0">
        <p className={clsx('text-2xl font-bold leading-tight', c.text)}>{value}</p>
        <p className="text-xs font-medium text-muted-foreground mt-0.5 truncate">{label}</p>
      </div>
    </div>
  );
}
