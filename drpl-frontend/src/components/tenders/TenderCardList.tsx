import type { Tender } from '../../types/tender';
import TenderCard from './TenderCard';
import EmptyState from '../ui/EmptyState';

interface TenderCardListProps {
  tenders: Tender[];
  searchQuery?: string;
  selectedIds?: Set<number>;
  onToggleSelect?: (id: number) => void;
  onToggleAll?: () => void;
  variant?: 'default' | 'discarded';
}

export default function TenderCardList({ tenders, searchQuery, selectedIds, onToggleSelect, onToggleAll, variant = 'default' }: TenderCardListProps) {
  if (tenders.length === 0) {
    return <EmptyState message="No tenders found matching your filters" />;
  }
  const selectable = !!onToggleSelect;
  const allSelected = selectable && tenders.every(t => selectedIds?.has(t.id));

  return (
    <div className="space-y-3">
      {selectable && (
        <label className="flex items-center gap-2 text-xs text-muted-foreground font-medium px-1">
          <input
            type="checkbox"
            checked={!!allSelected}
            onChange={onToggleAll}
            className="h-4 w-4 rounded border-input text-accent focus:ring-ring cursor-pointer"
            aria-label="Select all visible tenders"
          />
          Select all visible
        </label>
      )}
      {tenders.map(t => (
        <TenderCard
          key={t.id}
          tender={t}
          searchQuery={searchQuery}
          selectable={selectable}
          selected={selectedIds?.has(t.id)}
          onToggleSelect={onToggleSelect}
          variant={variant}
        />
      ))}
    </div>
  );
}
