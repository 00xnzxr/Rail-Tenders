import { Brain, Tag, Clock, Trash2 } from 'lucide-react';

interface Memory {
  id: number;
  agent_key?: string;
  memory_type: string;
  content: string;
  keywords: string[];
  importance: number;
  created_at: string;
}

interface MemoryPanelProps {
  memories: Memory[];
  onDelete?: (id: number) => void;
  title?: string;
}

const TYPE_COLORS: Record<string, string> = {
  fact: 'bg-accent/15 text-accent',
  preference: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  learning: 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400',
  decision: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
};

export default function MemoryPanel({ memories, onDelete, title = 'Agent Memory' }: MemoryPanelProps) {
  if (memories.length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        <Brain size={32} className="mx-auto mb-2 opacity-50" />
        <p className="text-sm">No memories stored yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
        <Brain size={16} />
        {title} ({memories.length})
      </h3>

      <div className="space-y-2 max-h-96 overflow-y-auto">
        {memories.map((mem) => (
          <div key={mem.id} className="bg-card border border-border rounded-lg p-3 text-sm">
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${TYPE_COLORS[mem.memory_type] || 'bg-muted text-muted-foreground'}`}>
                    {mem.memory_type}
                  </span>
                  {mem.agent_key && (
                    <span className="text-xs text-muted-foreground">{mem.agent_key}</span>
                  )}
                </div>
                <p className="text-foreground leading-snug">{mem.content}</p>
                {mem.keywords && mem.keywords.length > 0 && (
                  <div className="flex items-center gap-1 mt-1.5 flex-wrap">
                    <Tag size={12} className="text-muted-foreground" />
                    {mem.keywords.map((kw, i) => (
                      <span key={i} className="text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded">
                        {kw}
                      </span>
                    ))}
                  </div>
                )}
                <div className="flex items-center gap-2 mt-1.5 text-xs text-muted-foreground">
                  <Clock size={11} />
                  {new Date(mem.created_at).toLocaleDateString()}
                  <span className="ml-auto">importance: {mem.importance}</span>
                </div>
              </div>
              {onDelete && (
                <button
                  onClick={() => onDelete(mem.id)}
                  className="text-muted-foreground/50 hover:text-red-500 transition-colors p-1"
                >
                  <Trash2 size={14} />
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
