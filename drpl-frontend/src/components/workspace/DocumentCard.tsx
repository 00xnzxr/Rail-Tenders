import { FileText, Zap, FileSearch, Paperclip, CheckCircle2, Clock, Edit3, Eye, XCircle, Bot } from 'lucide-react';
import FormatTemplateBadge from './FormatTemplateBadge';
import type { WorkspaceItem, DocumentFormatTemplate } from '../../types/workspace';

interface Props {
  item: WorkspaceItem;
  templateMap?: Record<number, DocumentFormatTemplate>;
  onOpen: (id: number) => void;
  onToggleNotRequired: (id: number) => void;
}

const CATEGORY_CONFIG: Record<string, { label: string; color: string; bg: string; icon: typeof FileText }> = {
  standard: { label: 'Standard', color: 'text-muted-foreground', bg: 'bg-muted', icon: Paperclip },
  generated: { label: 'Generated', color: 'text-accent', bg: 'bg-accent/10', icon: Zap },
  analysis: { label: 'Analysis', color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-500/15', icon: FileSearch },
};

const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string; icon: typeof Clock }> = {
  not_started: { label: 'Not Started', color: 'text-muted-foreground', bg: 'bg-muted', icon: Clock },
  drafting: { label: 'Drafting', color: 'text-accent', bg: 'bg-accent/10', icon: Edit3 },
  in_review: { label: 'In Review', color: 'text-amber-600 dark:text-amber-400', bg: 'bg-amber-50 dark:bg-amber-500/15', icon: Eye },
  approved: { label: 'Approved', color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-50 dark:bg-emerald-500/15', icon: CheckCircle2 },
  rejected: { label: 'Rejected', color: 'text-red-600 dark:text-red-400', bg: 'bg-red-50 dark:bg-red-500/15', icon: XCircle },
};

export default function DocumentCard({ item, templateMap, onOpen, onToggleNotRequired }: Props) {
  const category = CATEGORY_CONFIG[item.item_category] || CATEGORY_CONFIG.standard;
  const status = STATUS_CONFIG[item.review_status] || STATUS_CONFIG.not_started;
  const StatusIcon = status.icon;
  const isNotRequired = item.is_not_required;

  return (
    <div
      className={`group relative border rounded-xl p-4 transition-all hover:shadow-md cursor-pointer ${
        isNotRequired
          ? 'border-border bg-muted/40 opacity-60'
          : item.review_status === 'approved'
          ? 'border-emerald-200 dark:border-emerald-500/20 bg-emerald-50/30'
          : 'border-border bg-card hover:border-indigo-300'
      }`}
      onClick={() => onOpen(item.id)}
    >
      {/* Top row: category badge + status */}
      <div className="flex items-center justify-between mb-3">
        <span className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${category.bg} ${category.color}`}>
          <category.icon size={12} />
          {category.label}
        </span>
        <span className={`inline-flex items-center gap-1 text-xs font-medium ${status.color}`}>
          <StatusIcon size={12} />
          {isNotRequired ? 'Not Required' : status.label}
        </span>
      </div>

      {/* Document name */}
      <h3 className="text-sm font-semibold text-foreground line-clamp-2 mb-2 leading-tight">
        {item.item_name}
      </h3>

      {/* Description snippet */}
      {item.item_description && (
        <p className="text-xs text-muted-foreground line-clamp-2 mb-3">
          {item.item_description}
        </p>
      )}

      {/* Bottom row: agent + template + version */}
      <div className="flex items-center justify-between mt-auto pt-2 border-t border-border">
        <div className="flex items-center gap-1 min-w-0 overflow-hidden">
          {item.agent_key && (
            <span className="inline-flex items-center gap-1 text-xs text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-500/15 px-1.5 py-0.5 rounded flex-shrink-0">
              <Bot size={10} />
              DRPL-assisted
            </span>
          )}
          {(item.format_template_id || item.format_instructions) && (
            <FormatTemplateBadge
              template={item.format_template_id && templateMap?.[item.format_template_id]
                ? templateMap[item.format_template_id]
                : null}
              formatInstructions={item.format_instructions}
              size="sm"
            />
          )}
        </div>
        {item.content_version > 0 && (
          <span className="text-xs text-muted-foreground flex-shrink-0">v{item.content_version}</span>
        )}
      </div>

      {/* Not required toggle (for standard items) */}
      {item.item_category === 'standard' && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggleNotRequired(item.id);
          }}
          className="absolute right-2 top-2 rounded-full border bg-card px-2 py-0.5 text-xs text-muted-foreground opacity-100 shadow-sm transition-opacity hover:text-foreground sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
        >
          {isNotRequired ? 'Mark Required' : 'Not Required'}
        </button>
      )}
    </div>
  );
}
