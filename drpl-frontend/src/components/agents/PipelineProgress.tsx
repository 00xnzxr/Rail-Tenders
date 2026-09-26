import { CheckCircle2, Circle, Loader2, XCircle, FileSearch, ListChecks, FileText, Calculator, Layers } from 'lucide-react';
import clsx from 'clsx';

interface PipelineStep {
  key: string;
  label: string;
  icon: any;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
}

interface PipelineProgressProps {
  completedSteps: string[];
  currentStep?: string;
  errors?: string[];
}

const PIPELINE_STEPS = [
  { key: 'analyze_documents', label: 'Document Analysis', icon: FileSearch },
  { key: 'generate_checklist', label: 'Checklist Generation', icon: ListChecks },
  { key: 'generate_documents', label: 'Document Generation', icon: FileText },
  { key: 'research_costing', label: 'Costing Research', icon: Calculator },
  { key: 'workspace_setup', label: 'Workspace', icon: Layers },
];

export default function PipelineProgress({ completedSteps, currentStep, errors = [] }: PipelineProgressProps) {
  const steps: PipelineStep[] = PIPELINE_STEPS.map((step) => {
    const completed = completedSteps.some(s => s.startsWith(step.key));
    const failed = completedSteps.some(s => s.includes(step.key) && s.includes('failed'));
    const skipped = completedSteps.some(s => s.includes(step.key) && s.includes('skipped'));
    const running = currentStep === step.key;

    let status: PipelineStep['status'] = 'pending';
    if (failed) status = 'failed';
    else if (skipped) status = 'skipped';
    else if (completed) status = 'completed';
    else if (running) status = 'running';

    return { ...step, status };
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {steps.map((step, i) => (
          <div key={step.key} className="flex items-center">
            <div className={clsx(
              'flex items-center gap-2 px-3 py-2 rounded-lg border text-sm',
              step.status === 'completed' && 'bg-green-50 dark:bg-green-500/15 border-green-200 dark:border-green-500/20 text-green-700 dark:text-green-400',
              step.status === 'running' && 'bg-accent/10 border-accent/20 text-accent',
              step.status === 'failed' && 'bg-red-50 dark:bg-red-500/15 border-red-200 dark:border-red-500/20 text-red-700 dark:text-red-400',
              step.status === 'skipped' && 'bg-yellow-50 dark:bg-yellow-500/15 border-yellow-200 dark:border-yellow-500/20 text-yellow-700 dark:text-yellow-400',
              step.status === 'pending' && 'bg-muted/40 border-border text-muted-foreground',
            )}>
              {step.status === 'completed' && <CheckCircle2 size={16} />}
              {step.status === 'running' && <Loader2 size={16} className="animate-spin" />}
              {step.status === 'failed' && <XCircle size={16} />}
              {step.status === 'pending' && <Circle size={16} />}
              {step.status === 'skipped' && <Circle size={16} />}
              <step.icon size={16} />
              <span className="font-medium">{step.label}</span>
            </div>
            {i < steps.length - 1 && (
              <div className={clsx(
                'w-8 h-0.5 mx-1',
                steps[i + 1].status !== 'pending' ? 'bg-green-300' : 'bg-muted'
              )} />
            )}
          </div>
        ))}
      </div>

      {errors.length > 0 && (
        <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-3">
          <p className="text-sm font-medium text-red-700 dark:text-red-400 mb-1">Pipeline Errors</p>
          {errors.map((err, i) => (
            <p key={i} className="text-xs text-red-600 dark:text-red-400">{err}</p>
          ))}
        </div>
      )}
    </div>
  );
}
