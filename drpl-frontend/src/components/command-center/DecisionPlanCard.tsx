/**
 * DecisionPlanCard — inline approval card shown when the decision_maker has
 * proposed a plan and is waiting for the user's decision.
 *
 * Three actions:
 *   - Approve → resumes execution with the plan as-is
 *   - Change  → opens a textarea for feedback, then resumes planning
 *   - Deny    → opens a textarea for an optional reason, then cancels
 *
 * When any button is clicked, the parent calls `onRespond(action, feedback?)`
 * and the card goes into a disabled / "responded" state to prevent duplicates.
 */

import { useState } from 'react';
import {
  CheckCircle2, Pencil, XCircle, Sparkles, Loader2,
} from 'lucide-react';

export interface DecisionPlanStep {
  description: string;
  agent?: string | null;
  rationale?: string | null;
}

export interface DecisionPlan {
  title: string;
  reasoning: string;
  steps: DecisionPlanStep[];
}

export interface DecisionPlanCardProps {
  plan: DecisionPlan;
  disabled?: boolean;
  onRespond: (action: 'approve' | 'change' | 'deny', feedback?: string) => void;
}

const AGENT_LABELS: Record<string, string> = {
  deep_analyzer: 'Deep Analyzer',
  checklist_generator: 'Checklist Generator',
  proposal_creator: 'Proposal Creator',
  costing_researcher: 'Costing Researcher',
  annexure_finder: 'Annexure Finder',
  workspace_manager: 'Workspace Manager',
};

function agentLabel(key?: string | null): string | null {
  if (!key) return null;
  return AGENT_LABELS[key] || key;
}

export default function DecisionPlanCard({ plan, disabled, onRespond }: DecisionPlanCardProps) {
  const [panel, setPanel] = useState<'idle' | 'change' | 'deny'>('idle');
  const [feedback, setFeedback] = useState('');
  const [submitted, setSubmitted] = useState<null | 'approve' | 'change' | 'deny'>(null);

  const isDone = disabled || submitted !== null;

  function submit(action: 'approve' | 'change' | 'deny') {
    if (isDone) return;
    setSubmitted(action);
    onRespond(action, feedback.trim() || undefined);
  }

  return (
    <div className="my-2 rounded-xl border border-violet-200 bg-gradient-to-br from-violet-50 to-white shadow-sm">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-violet-100 px-4 py-3">
        <Sparkles size={16} className="text-violet-600" />
        <span className="text-sm font-semibold text-violet-900">Proposed Plan</span>
        {isDone && submitted && (
          <span className="ml-auto text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {submitted === 'approve' && 'Approved'}
            {submitted === 'change' && 'Changes requested'}
            {submitted === 'deny' && 'Denied'}
          </span>
        )}
      </div>

      {/* Body */}
      <div className="px-4 py-3">
        <h3 className="text-base font-semibold text-foreground">{plan.title || 'Proposed Plan'}</h3>
        {plan.reasoning && (
          <p className="mt-2 text-sm leading-relaxed text-foreground">{plan.reasoning}</p>
        )}

        {plan.steps && plan.steps.length > 0 && (
          <ol className="mt-3 space-y-2">
            {plan.steps.map((step, i) => (
              <li key={i} className="flex gap-3">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-violet-600 text-xs font-semibold text-white">
                  {i + 1}
                </span>
                <div className="flex-1">
                  <div className="text-sm text-foreground">{step.description}</div>
                  {(agentLabel(step.agent) || step.rationale) && (
                    <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      {agentLabel(step.agent) && (
                        <span className="rounded-full bg-muted px-2 py-0.5 font-medium">
                          {agentLabel(step.agent)}
                        </span>
                      )}
                      {step.rationale && <span className="italic">{step.rationale}</span>}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>

      {/* Feedback panel (appears when Change or Deny clicked) */}
      {!isDone && panel !== 'idle' && (
        <div className="border-t border-violet-100 bg-card px-4 py-3">
          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {panel === 'change' ? 'What should change?' : 'Why are you denying this plan? (optional)'}
          </label>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder={
              panel === 'change'
                ? 'e.g. Skip the deep analysis, just extract annexures directly.'
                : 'Optional — helps the agent understand what went wrong.'
            }
            rows={3}
            className="mt-1.5 block w-full resize-y rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-violet-400 focus:outline-none focus:ring-2 focus:ring-violet-200"
            autoFocus
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            <button
              onClick={() => { setPanel('idle'); setFeedback(''); }}
              className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground hover:bg-muted/40"
            >
              Cancel
            </button>
            <button
              onClick={() => submit(panel)}
              disabled={panel === 'change' && feedback.trim().length === 0}
              className={`rounded-md px-3 py-1.5 text-xs font-semibold text-white ${
                panel === 'change'
                  ? 'bg-amber-600 hover:bg-amber-700 disabled:bg-amber-300'
                  : 'bg-red-600 hover:bg-red-700'
              }`}
            >
              {panel === 'change' ? 'Submit changes' : 'Confirm deny'}
            </button>
          </div>
        </div>
      )}

      {/* Action bar */}
      {!isDone && panel === 'idle' && (
        <div className="flex items-center justify-end gap-2 border-t border-violet-100 bg-card px-4 py-2.5">
          <button
            onClick={() => setPanel('deny')}
            className="inline-flex items-center gap-1.5 rounded-md border border-red-200 dark:border-red-500/20 bg-card px-3 py-1.5 text-sm font-medium text-red-700 dark:text-red-400 hover:bg-red-50 dark:bg-red-500/15"
          >
            <XCircle size={14} /> Deny
          </button>
          <button
            onClick={() => setPanel('change')}
            className="inline-flex items-center gap-1.5 rounded-md border border-amber-200 dark:border-amber-500/20 bg-card px-3 py-1.5 text-sm font-medium text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:bg-amber-500/15"
          >
            <Pencil size={14} /> Request changes
          </button>
          <button
            onClick={() => submit('approve')}
            className="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-700"
          >
            <CheckCircle2 size={14} /> Approve
          </button>
        </div>
      )}

      {/* Disabled / submitted footer */}
      {isDone && submitted && (
        <div className="flex items-center gap-2 border-t border-violet-100 bg-muted/40 px-4 py-2 text-xs text-muted-foreground">
          {submitted === 'approve' && (
            <>
              <Loader2 size={12} className="animate-spin text-emerald-600 dark:text-emerald-400" />
              Executing the plan…
            </>
          )}
          {submitted === 'change' && (
            <>
              <Loader2 size={12} className="animate-spin text-amber-600 dark:text-amber-400" />
              Revising the plan based on your feedback…
            </>
          )}
          {submitted === 'deny' && <span>Plan rejected. You can send a new request above.</span>}
        </div>
      )}
    </div>
  );
}
