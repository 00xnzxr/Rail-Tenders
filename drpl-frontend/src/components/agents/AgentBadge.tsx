/**
 * AgentBadge - Small pill showing which agent produced a response.
 */

import {
  Search, ClipboardList, FileText, DollarSign, Bot, Cpu,
  Layers, FileSpreadsheet, Sparkles,
} from 'lucide-react';

interface AgentBadgeProps {
  // Tolerate null/undefined/non-string — we fall back to the "general" config
  // instead of crashing the whole page when an upstream field is missing.
  agentKey?: string | null;
  displayName?: string;
  size?: 'sm' | 'md';
}

const AGENT_CONFIG: Record<string, { icon: any; label: string; color: string }> = {
  deep_analyzer: {
    icon: Search,
    label: 'Deep Analyzer',
    color: 'bg-purple-50 dark:bg-purple-500/15 text-purple-700 dark:text-purple-400 border-purple-200 dark:border-purple-500/20',
  },
  checklist_generator: {
    icon: ClipboardList,
    label: 'Checklist Generator',
    color: 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20',
  },
  proposal_creator: {
    icon: FileText,
    label: 'Proposal Creator',
    color: 'bg-accent/10 text-accent border-accent/20',
  },
  costing_researcher: {
    icon: DollarSign,
    label: 'Costing Researcher',
    color: 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-200 dark:border-amber-500/20',
  },
  workspace_manager: {
    icon: Layers,
    label: 'Workspace Manager',
    color: 'bg-muted/40 text-foreground border-border',
  },
  annexure_finder: {
    icon: FileSpreadsheet,
    label: 'Annexure Finder',
    color: 'bg-indigo-50 dark:bg-indigo-500/15 text-indigo-700 dark:text-indigo-400 border-indigo-200 dark:border-indigo-500/20',
  },
  decision_maker: {
    icon: Sparkles,
    label: 'Decision Maker',
    color: 'bg-violet-50 text-violet-700 border-violet-200',
  },
  proposal_router: {
    icon: Cpu,
    label: 'Router',
    color: 'bg-muted/40 text-muted-foreground border-border',
  },
  general: {
    icon: Bot,
    label: 'DRPL Assistant',
    color: 'bg-muted/40 text-muted-foreground border-border',
  },
};

export default function AgentBadge({ agentKey, displayName, size = 'sm' }: AgentBadgeProps) {
  // Guard: treat non-string / empty as "general" so a missing field never
  // crashes the whole chat view.
  const safeKey = typeof agentKey === 'string' ? agentKey : '';
  // Handle comma-separated agent keys (multi-agent)
  const agentKeys = safeKey.split(',').map(k => k.trim()).filter(Boolean);
  const primaryKey = agentKeys[0] || 'general';
  const config = AGENT_CONFIG[primaryKey] || AGENT_CONFIG.general;
  const Icon = config.icon;
  const label = displayName || config.label;

  const sizeClasses = size === 'sm'
    ? 'px-2 py-0.5 text-[10px] gap-1'
    : 'px-2.5 py-1 text-xs gap-1.5';

  const iconSize = size === 'sm' ? 10 : 12;

  const isMulti = agentKeys.length > 1;

  return (
    <div className="flex items-center gap-1">
      <span className={`inline-flex items-center border rounded-full font-medium ${sizeClasses} ${config.color}`}>
        <Icon size={iconSize} />
        {label}
      </span>
      {isMulti && (
        <span className="inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium bg-muted text-muted-foreground rounded-full border border-border">
          +{agentKeys.length - 1}
        </span>
      )}
    </div>
  );
}
