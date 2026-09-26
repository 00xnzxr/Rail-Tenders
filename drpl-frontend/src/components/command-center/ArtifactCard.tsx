import { FileText, BarChart3, ClipboardList, Search, ArrowRight, Sparkles, FileSpreadsheet } from 'lucide-react';
import type { ArtifactCreatedEvent } from '../../types/command-center';

const TYPE_CONFIG: Record<string, { icon: any; label: string; accent: string; bg: string; border: string }> = {
  document: {
    icon: FileText,
    label: 'Document Generated',
    accent: 'text-accent',
    bg: 'bg-accent/10',
    border: 'border-accent/20',
  },
  cost_breakdown: {
    icon: BarChart3,
    label: 'Cost Breakdown Ready',
    accent: 'text-amber-600 dark:text-amber-400',
    bg: 'bg-amber-50 dark:bg-amber-500/15',
    border: 'border-amber-100',
  },
  cost_breakdown_xlsx: {
    icon: FileSpreadsheet,
    label: 'Cost Spreadsheet (.xlsx)',
    accent: 'text-emerald-600 dark:text-emerald-400',
    bg: 'bg-emerald-50 dark:bg-emerald-500/15',
    border: 'border-emerald-100',
  },
  checklist: {
    icon: ClipboardList,
    label: 'Checklist Generated',
    accent: 'text-emerald-600 dark:text-emerald-400',
    bg: 'bg-emerald-50 dark:bg-emerald-500/15',
    border: 'border-emerald-100',
  },
  analysis: {
    icon: Search,
    label: 'Analysis Complete',
    accent: 'text-purple-600 dark:text-purple-400',
    bg: 'bg-purple-50 dark:bg-purple-500/15',
    border: 'border-purple-100',
  },
};

interface ArtifactCardProps {
  artifactRef: ArtifactCreatedEvent;
  onOpen: (artifactId: number) => void;
}

export default function ArtifactCard({ artifactRef, onOpen }: ArtifactCardProps) {
  const cfg = TYPE_CONFIG[artifactRef.artifact_type] || TYPE_CONFIG.document;
  const Icon = cfg.icon;

  return (
    <button
      onClick={() => onOpen(artifactRef.artifact_id)}
      className={`
        group w-full flex items-center gap-3 px-4 py-3 mt-2
        rounded-xl border ${cfg.border} ${cfg.bg}
        hover:shadow-md hover:scale-[1.01] hover:-translate-y-px active:scale-[0.99]
        transition-all duration-150 text-left
        animate-fade-slide-up
      `}
    >
      {/* Icon badge */}
      <div className={`flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center bg-card shadow-sm border ${cfg.border}`}>
        <Icon size={16} className={cfg.accent} />
      </div>

      {/* Text */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <Sparkles size={11} className={`${cfg.accent} flex-shrink-0`} />
          <span className={`text-xs font-semibold uppercase tracking-wide ${cfg.accent}`}>
            {cfg.label}
          </span>
          {artifactRef.version > 1 && (
            <span className="text-xs text-muted-foreground font-normal">v{artifactRef.version}</span>
          )}
        </div>
        <p className="text-sm font-medium text-foreground truncate mt-0.5">{artifactRef.title}</p>
      </div>

      {/* Arrow */}
      <div className={`flex-shrink-0 flex items-center gap-1 text-xs font-medium ${cfg.accent} opacity-70 group-hover:opacity-100 transition-opacity`}>
        <span>Open</span>
        <ArrowRight size={13} className="group-hover:translate-x-0.5 transition-transform" />
      </div>
    </button>
  );
}
