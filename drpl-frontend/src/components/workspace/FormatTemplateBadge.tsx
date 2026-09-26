import { FileText, Sparkles } from 'lucide-react';

interface Props {
  template: { name: string; document_category: string } | null;
  formatInstructions?: string | null;
  size?: 'sm' | 'md';
}

export default function FormatTemplateBadge({ template, formatInstructions, size = 'sm' }: Props) {
  const sizeClasses = size === 'sm'
    ? 'text-xs px-1.5 py-0.5'
    : 'text-sm px-2 py-1';

  if (template) {
    const displayName = size === 'sm' && template.name.length > 20
      ? template.name.slice(0, 20) + '...'
      : template.name;

    return (
      <span
        className={`inline-flex items-center gap-1 rounded ${sizeClasses} bg-teal-50 dark:bg-teal-500/15 text-teal-700 dark:text-teal-400`}
        title={template.name}
      >
        <FileText size={size === 'sm' ? 12 : 14} />
        {displayName}
      </span>
    );
  }

  if (formatInstructions) {
    return (
      <span
        className={`inline-flex items-center gap-1 rounded ${sizeClasses} bg-cyan-50 dark:bg-cyan-500/15 text-cyan-700 dark:text-cyan-400`}
        title={formatInstructions.length > 100 ? formatInstructions.slice(0, 100) + '...' : formatInstructions}
      >
        <Sparkles size={size === 'sm' ? 12 : 14} />
        DRPL format
      </span>
    );
  }

  return null;
}
