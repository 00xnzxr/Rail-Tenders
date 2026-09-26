import { Search, ClipboardList, FileText, DollarSign, Link, Send, HelpCircle, Table, Edit, Layers, LayoutGrid, ExternalLink } from 'lucide-react';
import type { Suggestion } from '../../types/command-center';

const ICON_MAP: Record<string, any> = {
  search: Search,
  'clipboard-list': ClipboardList,
  'file-text': FileText,
  'dollar-sign': DollarSign,
  link: Link,
  send: Send,
  'help-circle': HelpCircle,
  table: Table,
  edit: Edit,
  layers: Layers,
  'layout-grid': LayoutGrid,
  'external-link': ExternalLink,
};

interface SuggestionBarProps {
  suggestions: Suggestion[];
  onSelect: (suggestion: Suggestion) => void;
  disabled?: boolean;
}

export default function SuggestionBar({ suggestions, onSelect, disabled }: SuggestionBarProps) {
  if (!suggestions.length) return null;

  return (
    <div className="flex gap-2 overflow-x-auto pb-1 px-1 scrollbar-thin">
      {suggestions.map((s, i) => {
        const Icon = ICON_MAP[s.icon] || HelpCircle;
        return (
          <button
            key={i}
            onClick={() => onSelect(s)}
            disabled={disabled}
            className="flex items-center gap-2 px-3 py-1.5 rounded-full border border-border bg-card text-sm text-muted-foreground hover:bg-muted/40 hover:border-border transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Icon size={14} className="text-muted-foreground flex-shrink-0" />
            <span className="truncate max-w-[200px]">{s.text}</span>
          </button>
        );
      })}
    </div>
  );
}
