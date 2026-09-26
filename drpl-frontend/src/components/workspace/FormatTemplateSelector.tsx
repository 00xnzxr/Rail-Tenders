import { useState, useEffect, useMemo } from 'react';
import { X, Check, Lock, FileText, Search } from 'lucide-react';
import { getFormatTemplates } from '../../lib/api';
import type { DocumentFormatTemplate } from '../../types/workspace';

const CATEGORY_COLORS: Record<string, string> = {
  annexure: 'bg-accent/10 text-accent',
  declaration: 'bg-purple-50 dark:bg-purple-500/15 text-purple-700 dark:text-purple-400',
  certificate: 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-400',
  boq: 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400',
  letter: 'bg-indigo-50 dark:bg-indigo-500/15 text-indigo-700 dark:text-indigo-400',
  proposal: 'bg-rose-50 dark:bg-rose-500/15 text-rose-700 dark:text-rose-400',
  custom: 'bg-muted text-foreground',
};

interface Props {
  tenderId: number;
  currentTemplateId: number | null;
  itemCategory: string;
  onSelect: (templateId: number | null) => void;
  onClose: () => void;
}

export default function FormatTemplateSelector({
  tenderId,
  currentTemplateId,
  itemCategory,
  onSelect,
  onClose,
}: Props) {
  const [templates, setTemplates] = useState<DocumentFormatTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(currentTemplateId);
  const [filterCategory, setFilterCategory] = useState('all');
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    const load = async () => {
      try {
        const data = await getFormatTemplates(tenderId);
        setTemplates(data);
      } catch {
        // Ignore
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [tenderId]);

  const categories = useMemo(() => {
    const cats = new Set(templates.map((t) => t.document_category));
    return Array.from(cats).sort();
  }, [templates]);

  const filtered = useMemo(() => {
    return templates.filter((t) => {
      if (filterCategory !== 'all' && t.document_category !== filterCategory) return false;
      if (searchQuery) {
        const q = searchQuery.toLowerCase();
        return (
          t.name.toLowerCase().includes(q) ||
          (t.description || '').toLowerCase().includes(q)
        );
      }
      return true;
    });
  }, [templates, filterCategory, searchQuery]);

  const handleApply = () => {
    onSelect(selectedId);
    onClose();
  };

  const handleRemove = () => {
    onSelect(null);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-card rounded-xl shadow-xl w-full max-w-2xl mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h3 className="text-lg font-semibold text-foreground">Select Format Template</h3>
          <button onClick={onClose} className="p-1 hover:bg-muted rounded-lg transition-colors">
            <X size={20} className="text-muted-foreground" />
          </button>
        </div>

        {/* Search */}
        <div className="px-6 pt-4">
          <div className="relative">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search templates..."
              className="w-full pl-9 pr-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
            />
          </div>
        </div>

        {/* Category filter pills */}
        <div className="px-6 pt-3 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => setFilterCategory('all')}
            className={`px-3 py-1 text-xs rounded-full transition-colors ${
              filterCategory === 'all'
                ? 'bg-indigo-600 text-white'
                : 'bg-muted text-muted-foreground hover:bg-muted'
            }`}
          >
            All
          </button>
          {categories.map((cat) => (
            <button
              key={cat}
              onClick={() => setFilterCategory(cat)}
              className={`px-3 py-1 text-xs rounded-full capitalize transition-colors ${
                filterCategory === cat
                  ? 'bg-indigo-600 text-white'
                  : 'bg-muted text-muted-foreground hover:bg-muted'
              }`}
            >
              {cat}
            </button>
          ))}
        </div>

        {/* Template list */}
        <div className="px-6 py-4 max-h-96 overflow-y-auto space-y-2">
          {loading && (
            <div className="text-center py-8 text-sm text-muted-foreground">Loading templates...</div>
          )}

          {!loading && filtered.length === 0 && (
            <div className="text-center py-8 text-sm text-muted-foreground">
              No templates found matching your filters.
            </div>
          )}

          {filtered.map((template) => {
            const isSelected = template.id === selectedId;
            const catColor = CATEGORY_COLORS[template.document_category] || CATEGORY_COLORS.custom;

            return (
              <button
                key={template.id}
                onClick={() => setSelectedId(template.id)}
                className={`w-full text-left p-3 rounded-lg border-2 transition-colors ${
                  isSelected
                    ? 'border-indigo-500 bg-indigo-50/50'
                    : 'border-transparent bg-muted/40 hover:bg-muted'
                }`}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      {isSelected && <Check size={14} className="text-indigo-600 dark:text-indigo-400 flex-shrink-0" />}
                      {template.is_system && <Lock size={12} className="text-muted-foreground flex-shrink-0" />}
                      <span className="font-medium text-foreground text-sm">{template.name}</span>
                    </div>
                    {template.description && (
                      <p className="text-xs text-muted-foreground mt-1 line-clamp-2 ml-0">
                        {template.description}
                      </p>
                    )}
                    <div className="flex items-center gap-2 mt-2">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded capitalize ${catColor}`}>
                        {template.document_category}
                      </span>
                      {template.required_sections.length > 0 && (
                        <span className="text-[10px] text-muted-foreground">
                          {template.required_sections.length} sections
                        </span>
                      )}
                      {template.format_rules.length > 0 && (
                        <span className="text-[10px] text-muted-foreground">
                          {template.format_rules.length} rules
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </button>
            );
          })}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t bg-muted/40 rounded-b-xl">
          <button
            onClick={handleRemove}
            className="text-sm text-muted-foreground hover:text-red-600 dark:text-red-400 transition-colors"
          >
            Remove Template
          </button>
          <div className="flex gap-3">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleApply}
              className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700 transition-colors"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
