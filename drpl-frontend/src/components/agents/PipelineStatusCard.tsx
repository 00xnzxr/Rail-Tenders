import { CheckCircle2, Circle, FileSearch, ListChecks, FileText, Calculator, Layers } from 'lucide-react';

const PIPELINE_STEPS = [
  { key: 'analyze_documents', label: 'Analysis', icon: FileSearch },
  { key: 'generate_checklist', label: 'Checklist', icon: ListChecks },
  { key: 'generate_documents', label: 'Documents', icon: FileText },
  { key: 'research_costing', label: 'Costing', icon: Calculator },
  { key: 'workspace_setup', label: 'Workspace', icon: Layers },
];

interface PipelineStatusCardProps {
  completedSteps: string[];
}

export default function PipelineStatusCard({ completedSteps }: PipelineStatusCardProps) {
  const completedCount = PIPELINE_STEPS.filter(s =>
    completedSteps.some(c => c.startsWith(s.key))
  ).length;

  return (
    <div className="flex justify-center my-2">
      <div className="inline-flex items-center gap-1 bg-card border border-border rounded-full px-4 py-2 shadow-sm">
        <span className="text-xs font-medium text-muted-foreground mr-1.5">
          {completedCount}/{PIPELINE_STEPS.length} steps
        </span>
        {PIPELINE_STEPS.map((step, i) => {
          const done = completedSteps.some(c => c.startsWith(step.key));
          const Icon = step.icon;
          return (
            <div key={step.key} className="flex items-center">
              <div
                title={step.label}
                className={`
                  flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium
                  transition-all
                  ${done
                    ? 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-500/20'
                    : 'text-muted-foreground border border-transparent'
                  }
                `}
              >
                {done
                  ? <CheckCircle2 size={12} className="text-emerald-500" />
                  : <Circle size={12} />
                }
                <Icon size={12} />
                <span className="hidden sm:inline">{step.label}</span>
              </div>
              {i < PIPELINE_STEPS.length - 1 && (
                <div className={`w-3 h-px mx-0.5 ${done ? 'bg-emerald-300' : 'bg-muted'}`} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
