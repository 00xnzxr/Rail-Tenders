import { useState, useRef, useCallback } from 'react';
import { Clock, Edit3, Eye, CheckCircle2, XCircle, Bot, GripVertical } from 'lucide-react';
import FormatTemplateBadge from './FormatTemplateBadge';
import type { WorkspaceItem, DocumentFormatTemplate } from '../../types/workspace';

interface Props {
  items: WorkspaceItem[];
  templateMap?: Record<number, DocumentFormatTemplate>;
  onStatusChange: (itemId: number, newStatus: string) => Promise<void>;
  onOpen: (id: number) => void;
  onToggleNotRequired: (id: number) => void;
}

type StatusKey = 'not_started' | 'drafting' | 'in_review' | 'approved' | 'rejected';

const COLUMNS: {
  key: StatusKey;
  label: string;
  color: string;
  headerBg: string;
  dotColor: string;
  icon: typeof Clock;
}[] = [
  { key: 'not_started', label: 'Not Started', color: 'text-muted-foreground', headerBg: 'bg-muted/40', dotColor: 'bg-muted-foreground/40', icon: Clock },
  { key: 'drafting', label: 'Drafting', color: 'text-accent', headerBg: 'bg-accent/10', dotColor: 'bg-accent/100', icon: Edit3 },
  { key: 'in_review', label: 'In Review', color: 'text-amber-700 dark:text-amber-400', headerBg: 'bg-amber-50 dark:bg-amber-500/15', dotColor: 'bg-amber-500', icon: Eye },
  { key: 'approved', label: 'Approved', color: 'text-emerald-700 dark:text-emerald-400', headerBg: 'bg-emerald-50 dark:bg-emerald-500/15', dotColor: 'bg-emerald-500', icon: CheckCircle2 },
  { key: 'rejected', label: 'Rejected', color: 'text-red-700 dark:text-red-400', headerBg: 'bg-red-50 dark:bg-red-500/15', dotColor: 'bg-red-500', icon: XCircle },
];

const CATEGORY_COLORS: Record<string, string> = {
  standard: 'bg-muted text-muted-foreground',
  generated: 'bg-accent/10 text-accent',
  analysis: 'bg-amber-50 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400',
};

export default function WorkspaceKanbanBoard({
  items,
  templateMap,
  onStatusChange,
  onOpen,
  onToggleNotRequired,
}: Props) {
  const [draggedItemId, setDraggedItemId] = useState<number | null>(null);
  const [dragOverColumn, setDragOverColumn] = useState<string | null>(null);
  const dragCounterRef = useRef<Record<string, number>>({});

  // Group items by review_status
  const groups: Record<string, WorkspaceItem[]> = {};
  for (const col of COLUMNS) groups[col.key] = [];
  for (const item of items) {
    const status = item.review_status || 'not_started';
    if (groups[status]) {
      groups[status].push(item);
    } else {
      groups['not_started'].push(item);
    }
  }
  // Sort each group by display_order
  for (const key of Object.keys(groups)) {
    groups[key].sort((a, b) => a.display_order - b.display_order);
  }

  const handleDragStart = useCallback((e: React.DragEvent, itemId: number) => {
    e.dataTransfer.setData('text/plain', String(itemId));
    e.dataTransfer.effectAllowed = 'move';
    setDraggedItemId(itemId);
  }, []);

  const handleDragEnd = useCallback(() => {
    setDraggedItemId(null);
    setDragOverColumn(null);
    dragCounterRef.current = {};
  }, []);

  const handleDragEnter = useCallback((e: React.DragEvent, columnKey: string) => {
    e.preventDefault();
    if (!dragCounterRef.current[columnKey]) dragCounterRef.current[columnKey] = 0;
    dragCounterRef.current[columnKey]++;
    setDragOverColumn(columnKey);
  }, []);

  const handleDragLeave = useCallback((columnKey: string) => {
    if (!dragCounterRef.current[columnKey]) dragCounterRef.current[columnKey] = 0;
    dragCounterRef.current[columnKey]--;
    if (dragCounterRef.current[columnKey] <= 0) {
      dragCounterRef.current[columnKey] = 0;
      setDragOverColumn((prev) => (prev === columnKey ? null : prev));
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  const handleDrop = useCallback(
    async (e: React.DragEvent, targetStatus: string) => {
      e.preventDefault();
      setDragOverColumn(null);
      dragCounterRef.current = {};

      const itemIdStr = e.dataTransfer.getData('text/plain');
      if (!itemIdStr) return;

      const itemId = Number(itemIdStr);
      const item = items.find((i) => i.id === itemId);
      if (!item) return;

      // Skip no-op drop (same column)
      if (item.review_status === targetStatus) return;

      // Skip not-required items
      if (item.is_not_required) return;

      await onStatusChange(itemId, targetStatus);
    },
    [items, onStatusChange]
  );

  return (
    <div className="flex gap-4 overflow-x-auto pb-4" style={{ minHeight: 'calc(100vh - 280px)' }}>
      {COLUMNS.map((col) => {
        const columnItems = groups[col.key];
        const ColIcon = col.icon;
        const isOver = dragOverColumn === col.key;

        return (
          <div
            key={col.key}
            className={`w-56 flex-shrink-0 rounded-xl transition-all duration-200 ${
              isOver ? 'ring-2 ring-indigo-400 bg-indigo-50/30' : 'bg-muted/40/50'
            }`}
            onDragEnter={(e) => handleDragEnter(e, col.key)}
            onDragLeave={() => handleDragLeave(col.key)}
            onDragOver={handleDragOver}
            onDrop={(e) => handleDrop(e, col.key)}
          >
            {/* Column header */}
            <div className={`flex items-center justify-between px-3 py-2.5 rounded-t-xl ${col.headerBg}`}>
              <div className="flex items-center gap-2">
                <ColIcon size={14} className={col.color} />
                <span className={`text-xs font-semibold ${col.color}`}>{col.label}</span>
              </div>
              <span className="text-[10px] font-medium bg-card/70 text-muted-foreground px-1.5 py-0.5 rounded-full min-w-[20px] text-center">
                {columnItems.length}
              </span>
            </div>

            {/* Cards */}
            <div
              className="p-2 space-y-2 overflow-y-auto"
              style={{ maxHeight: 'calc(100vh - 340px)' }}
            >
              {columnItems.length === 0 && (
                <div className="text-center py-6 text-muted-foreground/50">
                  <p className="text-xs">No items</p>
                </div>
              )}

              {columnItems.map((item) => {
                const isDragging = draggedItemId === item.id;
                const catColor = CATEGORY_COLORS[item.item_category] || CATEGORY_COLORS.standard;

                return (
                  <div
                    key={item.id}
                    draggable={!item.is_not_required}
                    onDragStart={(e) => handleDragStart(e, item.id)}
                    onDragEnd={handleDragEnd}
                    onClick={() => onOpen(item.id)}
                    className={`group relative bg-card border rounded-lg p-2.5 cursor-pointer transition-all ${
                      isDragging
                        ? 'opacity-40 border-indigo-300 shadow-none'
                        : item.is_not_required
                        ? 'opacity-50 border-border'
                        : 'border-border hover:border-indigo-300 hover:shadow-sm'
                    }`}
                  >
                    {/* Drag handle hint */}
                    {!item.is_not_required && (
                      <GripVertical
                        size={12}
                        className="absolute top-2 right-1.5 text-muted-foreground/50 opacity-0 group-hover:opacity-100 transition-opacity"
                      />
                    )}

                    {/* Item name */}
                    <p className="text-xs font-medium text-foreground line-clamp-2 mb-1.5 pr-4 leading-tight">
                      {item.item_name}
                    </p>

                    {/* Bottom: category + agent + version */}
                    <div className="flex items-center gap-1 flex-wrap">
                      <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full ${catColor}`}>
                        {item.item_category}
                      </span>

                      {item.agent_key && (
                        <span className="inline-flex items-center gap-0.5 text-[10px] text-purple-600 dark:text-purple-400 bg-purple-50 dark:bg-purple-500/15 px-1 py-0.5 rounded">
                          <Bot size={8} />
                          {item.agent_key}
                        </span>
                      )}

                      {(item.format_template_id || item.format_instructions) && templateMap && (
                        <FormatTemplateBadge
                          template={
                            item.format_template_id && templateMap[item.format_template_id]
                              ? templateMap[item.format_template_id]
                              : null
                          }
                          formatInstructions={item.format_instructions}
                          size="sm"
                        />
                      )}

                      {item.content_version > 0 && (
                        <span className="text-[10px] text-muted-foreground ml-auto">v{item.content_version}</span>
                      )}
                    </div>

                    {/* Not required toggle */}
                    {item.item_category === 'standard' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onToggleNotRequired(item.id);
                        }}
                        className="absolute bottom-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity text-[9px] text-muted-foreground hover:text-muted-foreground bg-card rounded px-1 py-0.5 shadow-sm border"
                      >
                        {item.is_not_required ? 'Required' : 'Skip'}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
