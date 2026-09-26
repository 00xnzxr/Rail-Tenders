import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { X, Plus, Search, ArrowRight, Link2 } from 'lucide-react';
import type { WorkspaceItem } from '../../types/workspace';

const STATUS_COLORS: Record<string, string> = {
  not_started: 'bg-muted-foreground/40',
  drafting: 'bg-accent/100',
  in_review: 'bg-amber-500',
  approved: 'bg-emerald-500',
  rejected: 'bg-red-500',
};

interface Props {
  tenderId: number;
  itemId: number;
  allItems: WorkspaceItem[];
  dependsOn: number[];
  referencedBy: number[];
  onUpdateDependencies: (newDepsOn: number[]) => void;
}

export default function DependencyGraph({
  tenderId,
  itemId,
  allItems,
  dependsOn,
  referencedBy,
  onUpdateDependencies,
}: Props) {
  const navigate = useNavigate();
  const [showAddDropdown, setShowAddDropdown] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowAddDropdown(false);
        setSearchQuery('');
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const itemMap = new Map(allItems.map((item) => [item.id, item]));

  const handleRemoveDependency = (depId: number) => {
    onUpdateDependencies(dependsOn.filter((id) => id !== depId));
  };

  const handleAddDependency = (depId: number) => {
    if (!dependsOn.includes(depId)) {
      onUpdateDependencies([...dependsOn, depId]);
    }
    setShowAddDropdown(false);
    setSearchQuery('');
  };

  const availableItems = allItems.filter(
    (item) =>
      item.id !== itemId &&
      !dependsOn.includes(item.id) &&
      (!searchQuery || item.item_name.toLowerCase().includes(searchQuery.toLowerCase()))
  );

  const renderChip = (
    depId: number,
    editable: boolean,
  ) => {
    const item = itemMap.get(depId);
    if (!item) return null;

    const statusColor = STATUS_COLORS[item.review_status] || STATUS_COLORS.not_started;

    return (
      <div
        key={depId}
        className="inline-flex items-center gap-1.5 bg-card border rounded-lg px-2.5 py-1.5 text-sm group"
      >
        <span className={`w-2 h-2 rounded-full flex-shrink-0 ${statusColor}`} />
        <button
          onClick={() => navigate(`/tenders/${tenderId}/workspace/${depId}`)}
          className="text-foreground hover:text-indigo-600 dark:text-indigo-400 transition-colors truncate max-w-[180px]"
          title={item.item_name}
        >
          {item.item_name}
        </button>
        {item.content_version > 0 && (
          <span className="text-[10px] text-muted-foreground flex-shrink-0">v{item.content_version}</span>
        )}
        {editable && (
          <button
            onClick={() => handleRemoveDependency(depId)}
            className="opacity-0 group-hover:opacity-100 p-0.5 hover:bg-red-50 dark:bg-red-500/15 rounded transition-all flex-shrink-0"
            title="Remove dependency"
          >
            <X size={12} className="text-red-400 hover:text-red-600 dark:text-red-400" />
          </button>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-5">
      {/* Depends On */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <ArrowRight size={14} className="text-muted-foreground" />
          <h4 className="text-sm font-medium text-foreground">
            Depends On
            {dependsOn.length > 0 && (
              <span className="ml-1.5 text-xs font-normal text-muted-foreground">({dependsOn.length})</span>
            )}
          </h4>
        </div>

        <div className="flex flex-wrap gap-2">
          {dependsOn.length === 0 && (
            <span className="text-xs text-muted-foreground italic">No dependencies</span>
          )}
          {dependsOn.map((depId) => renderChip(depId, true))}
        </div>

        {/* Add dependency button + dropdown */}
        <div className="relative mt-2" ref={dropdownRef}>
          <button
            onClick={() => setShowAddDropdown(!showAddDropdown)}
            className="flex items-center gap-1 text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:text-indigo-400 transition-colors px-2 py-1 rounded hover:bg-indigo-50 dark:bg-indigo-500/15"
          >
            <Plus size={12} />
            Add Dependency
          </button>

          {showAddDropdown && (
            <div className="absolute top-full left-0 mt-1 w-72 bg-card border rounded-xl shadow-lg z-50 py-1">
              <div className="px-3 py-2">
                <div className="relative">
                  <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Search documents..."
                    className="w-full pl-8 pr-3 py-1.5 border rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                    autoFocus
                  />
                </div>
              </div>
              <div className="max-h-48 overflow-y-auto">
                {availableItems.length === 0 && (
                  <div className="px-3 py-2 text-xs text-muted-foreground">No documents available</div>
                )}
                {availableItems.slice(0, 20).map((item) => {
                  const statusColor = STATUS_COLORS[item.review_status] || STATUS_COLORS.not_started;
                  return (
                    <button
                      key={item.id}
                      onClick={() => handleAddDependency(item.id)}
                      className="w-full text-left px-3 py-2 text-sm hover:bg-muted/40 transition-colors flex items-center gap-2"
                    >
                      <span className={`w-2 h-2 rounded-full flex-shrink-0 ${statusColor}`} />
                      <span className="truncate text-foreground">{item.item_name}</span>
                      <span className="text-[10px] text-muted-foreground flex-shrink-0 capitalize">
                        {item.item_category}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Referenced By */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Link2 size={14} className="text-muted-foreground" />
          <h4 className="text-sm font-medium text-foreground">
            Referenced By
            {referencedBy.length > 0 && (
              <span className="ml-1.5 text-xs font-normal text-muted-foreground">({referencedBy.length})</span>
            )}
          </h4>
        </div>

        <div className="flex flex-wrap gap-2">
          {referencedBy.length === 0 && (
            <span className="text-xs text-muted-foreground italic">Not referenced by other documents</span>
          )}
          {referencedBy.map((depId) => renderChip(depId, false))}
        </div>
      </div>
    </div>
  );
}
