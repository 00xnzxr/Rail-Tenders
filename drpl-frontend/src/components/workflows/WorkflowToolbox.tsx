import {
  Play, Square, Bot, GitBranch, GitCompare, RefreshCw,
  UserCheck, Shuffle, Variable, Wrench, StickyNote,
  Repeat, GitFork,
} from 'lucide-react';
import type { NodeType } from '../../types/workflow';

interface PaletteItem {
  type: NodeType;
  label: string;
  description: string;
  category: string;
  icon: typeof Play;
  borderColor: string;
  bgColor: string;
}

const PALETTE: PaletteItem[] = [
  // Flow
  { type: 'start', label: 'Start', description: 'Entry point', category: 'Flow', icon: Play, borderColor: 'border-green-300', bgColor: 'bg-green-50 dark:bg-green-500/15' },
  { type: 'end', label: 'End', description: 'Terminal node', category: 'Flow', icon: Square, borderColor: 'border-purple-300', bgColor: 'bg-purple-50 dark:bg-purple-500/15' },
  // Agents
  { type: 'agent', label: 'Agent', description: 'Execute an AI agent', category: 'Agents', icon: Bot, borderColor: 'border-accent/40', bgColor: 'bg-accent/10' },
  { type: 'classify', label: 'Classify', description: 'Route by intent', category: 'Agents', icon: GitBranch, borderColor: 'border-amber-300', bgColor: 'bg-amber-50 dark:bg-amber-500/15' },
  // Logic
  { type: 'if_else', label: 'If / Else', description: 'Conditional branch', category: 'Logic', icon: GitCompare, borderColor: 'border-yellow-300', bgColor: 'bg-yellow-50 dark:bg-yellow-500/15' },
  { type: 'while_loop', label: 'While Loop', description: 'Iterate with condition', category: 'Logic', icon: RefreshCw, borderColor: 'border-orange-300', bgColor: 'bg-orange-50 dark:bg-orange-500/15' },
  { type: 'user_approval', label: 'Approval', description: 'Wait for user', category: 'Logic', icon: UserCheck, borderColor: 'border-indigo-300', bgColor: 'bg-indigo-50 dark:bg-indigo-500/15' },
  { type: 'for_each', label: 'For Each', description: 'Iterate over a list', category: 'Logic', icon: Repeat, borderColor: 'border-teal-300', bgColor: 'bg-teal-50 dark:bg-teal-500/15' },
  { type: 'parallel', label: 'Parallel', description: 'Run concurrently', category: 'Logic', icon: GitFork, borderColor: 'border-rose-300', bgColor: 'bg-rose-50 dark:bg-rose-500/15' },
  // Data
  { type: 'transform', label: 'Transform', description: 'Map/format data', category: 'Data', icon: Shuffle, borderColor: 'border-border', bgColor: 'bg-card' },
  { type: 'set_state', label: 'Set State', description: 'Assign variables', category: 'Data', icon: Variable, borderColor: 'border-border', bgColor: 'bg-muted/40' },
  // Tools
  { type: 'tool', label: 'Tool', description: 'Execute a tool', category: 'Tools', icon: Wrench, borderColor: 'border-emerald-300', bgColor: 'bg-emerald-50 dark:bg-emerald-500/15' },
  // Other
  { type: 'note', label: 'Note', description: 'Documentation', category: 'Other', icon: StickyNote, borderColor: 'border-border', bgColor: 'bg-yellow-50/50' },
];

const CATEGORIES = ['Flow', 'Agents', 'Logic', 'Data', 'Tools', 'Other'];

interface WorkflowToolboxProps {
  onAddNode: (type: NodeType) => void;
}

export default function WorkflowToolbox({ onAddNode }: WorkflowToolboxProps) {
  return (
    <div className="w-48 shrink-0 bg-card border-r border-border overflow-y-auto">
      <div className="p-3">
        <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Nodes</h3>
        {CATEGORIES.map((cat) => {
          const items = PALETTE.filter((p) => p.category === cat);
          if (items.length === 0) return null;
          return (
            <div key={cat} className="mb-3">
              <div className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">{cat}</div>
              <div className="space-y-1">
                {items.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.type}
                      onClick={() => onAddNode(item.type)}
                      draggable
                      onDragStart={(e) => {
                        e.dataTransfer.setData('application/workflow-node-type', item.type);
                        e.dataTransfer.effectAllowed = 'move';
                      }}
                      className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md border text-left text-xs transition-colors hover:shadow-sm ${item.borderColor} ${item.bgColor} hover:opacity-90`}
                      title={item.description}
                    >
                      <Icon size={12} className="shrink-0 text-muted-foreground" />
                      <span className="font-medium text-foreground truncate">{item.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
