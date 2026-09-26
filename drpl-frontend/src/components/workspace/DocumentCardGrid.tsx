import { Paperclip, Zap, FileSearch } from 'lucide-react';
import DocumentCard from './DocumentCard';
import type { WorkspaceItem, DocumentFormatTemplate } from '../../types/workspace';

interface Props {
  items: WorkspaceItem[];
  templateMap?: Record<number, DocumentFormatTemplate>;
  onOpen: (id: number) => void;
  onToggleNotRequired: (id: number) => void;
}

const SECTION_CONFIG: Record<string, { label: string; icon: typeof Paperclip; color: string }> = {
  standard: { label: 'Standard Documents', icon: Paperclip, color: 'text-foreground' },
  generated: { label: 'Generated Documents', icon: Zap, color: 'text-accent' },
  analysis: { label: 'Analysis Documents', icon: FileSearch, color: 'text-amber-700 dark:text-amber-400' },
};

export default function DocumentCardGrid({ items, templateMap, onOpen, onToggleNotRequired }: Props) {
  // Group items by category
  const groups: Record<string, WorkspaceItem[]> = {};
  for (const item of items) {
    const cat = item.item_category || 'standard';
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push(item);
  }

  // Ordered categories
  const categoryOrder = ['standard', 'generated', 'analysis'];
  const orderedCategories = categoryOrder.filter((cat) => groups[cat]?.length > 0);

  if (items.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        <p className="text-lg font-medium">No documents to prepare yet</p>
        <p className="text-sm mt-1">Prepare a checklist first, then return here to begin the document work.</p>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {orderedCategories.map((cat) => {
        const config = SECTION_CONFIG[cat] || SECTION_CONFIG.standard;
        const SectionIcon = config.icon;
        const categoryItems = groups[cat];

        return (
          <div key={cat}>
            {/* Section header */}
            <div className="flex items-center gap-2 mb-4">
              <SectionIcon size={18} className={config.color} />
              <h2 className={`text-base font-semibold ${config.color}`}>
                {config.label}
              </h2>
              <span className="text-xs text-muted-foreground bg-muted px-2 py-0.5 rounded-full">
                {categoryItems.length}
              </span>
            </div>

            {/* Card grid */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {categoryItems.map((item) => (
                <DocumentCard
                  key={item.id}
                  item={item}
                  templateMap={templateMap}
                  onOpen={onOpen}
                  onToggleNotRequired={onToggleNotRequired}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
