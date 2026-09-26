/**
 * PlanExecutionProgress — shows progress while the decision_maker is
 * executing an approved plan, one step at a time.
 *
 * Each step is one of the approved plan's items; the backend streams
 * `plan_step_progress` events that flip the step between running /
 * completed / failed / skipped. We render a small numbered list with
 * a status icon per step.
 */

import { CheckCircle2, Loader2, AlertCircle, MinusCircle, ArrowRight } from 'lucide-react';

export interface PlanProgressStep {
  index: number;
  total: number;
  agent?: string | null;
  agent_display_name?: string | null;
  description?: string;
  status: 'running' | 'completed' | 'failed' | 'skipped';
  error?: string | null;
}

export interface PlanExecutionProgressProps {
  title?: string | null;
  steps: PlanProgressStep[];
  isStreaming?: boolean;
  // When the backend pauses between steps, this describes the step that
  // would run next. Clicking the rendered button calls onNext().
  awaitingNext?: {
    index: number;
    total: number;
    description?: string;
    agent?: string | null;
  } | null;
  onNext?: () => void;
  isLoadingNext?: boolean;
}

function StatusIcon({ status }: { status: PlanProgressStep['status'] }) {
  if (status === 'running') return <Loader2 size={14} className="animate-spin text-violet-600" />;
  if (status === 'completed') return <CheckCircle2 size={14} className="text-emerald-600 dark:text-emerald-400" />;
  if (status === 'failed') return <AlertCircle size={14} className="text-red-600 dark:text-red-400" />;
  return <MinusCircle size={14} className="text-muted-foreground" />;
}

export default function PlanExecutionProgress({
  title, steps, isStreaming, awaitingNext, onNext, isLoadingNext,
}: PlanExecutionProgressProps) {
  if (!steps || steps.length === 0) return null;
  const total = steps[0]?.total || steps.length;
  const completedCount = steps.filter((s) => s.status === 'completed').length;

  return (
    <div className="my-2 rounded-2xl border border-border bg-card shadow-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
          {isStreaming && <Loader2 size={14} className="animate-spin" />}
          <span>{isStreaming ? 'Working through the plan' : 'Plan completed'}</span>
          {title && <span className="font-normal text-muted-foreground">— {title}</span>}
        </div>
        <span className="text-xs font-medium text-muted-foreground">
          {completedCount}/{total} steps
        </span>
      </div>
      <ol className="divide-y divide-border">
        {steps.map((step) => (
          <li key={step.index} className="flex gap-3 px-4 py-2">
            <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center">
              <StatusIcon status={step.status} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm text-foreground">
                <span className="font-medium">Step {step.index}</span>
              </div>
              {step.description && (
                <div className="mt-0.5 text-xs text-muted-foreground line-clamp-2">
                  {step.description}
                </div>
              )}
              {step.status === 'failed' && step.error && (
                <div className="mt-1 text-xs text-red-600 dark:text-red-400 italic">{step.error}</div>
              )}
              {step.status === 'skipped' && step.error && (
                <div className="mt-1 text-xs text-amber-700 dark:text-amber-400 italic">{step.error}</div>
              )}
            </div>
          </li>
        ))}
      </ol>
      {awaitingNext && onNext && (
        <div className="flex flex-col gap-2 border-t-2 border-emerald-200 dark:border-emerald-500/20 bg-emerald-50 dark:bg-emerald-500/15 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="text-sm text-foreground min-w-0">
            <div className="font-semibold text-emerald-900">
              ▶ Next: Step {awaitingNext.index}
            </div>
            {awaitingNext.description && (
              <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                {awaitingNext.description}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onNext}
            disabled={!!isLoadingNext}
            className="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-emerald-700 active:scale-[0.98] disabled:opacity-60"
          >
            {isLoadingNext ? (
              <><Loader2 size={16} className="animate-spin" /> Running step…</>
            ) : (
              <>Run step {awaitingNext.index} <ArrowRight size={16} /></>
            )}
          </button>
        </div>
      )}
    </div>
  );
}
