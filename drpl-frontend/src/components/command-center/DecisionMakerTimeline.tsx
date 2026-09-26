/**
 * DecisionMakerTimeline — renders the master agent's live reasoning trace
 * (Thought / Action / Observation steps) inline inside the assistant chat
 * bubble. Always visible (per product requirement), individual steps are
 * collapsible, and the budget pill updates in real time from SSE events.
 */

import { useMemo, useState } from 'react';
import {
  Brain, Wrench, Eye, ChevronDown, ChevronRight, Sparkles,
  AlertCircle, Clock,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { TimelineBudget, TimelineEvent } from '../../types/command-center';

interface DecisionMakerTimelineProps {
  timeline: TimelineEvent[];
  budget?: TimelineBudget;
  isStreaming?: boolean;
}

// Use lucide-react's own LucideIcon type — its props (size: string|number)
// don't fit the narrower ComponentType<{size?: number}> signature TS infers
// from inline literals, which broke the Vercel TS build.
const STEP_ICON: Record<TimelineEvent['type'], LucideIcon> = {
  thought: Brain,
  action: Wrench,
  observation: Eye,
};

const STEP_COLOR: Record<TimelineEvent['type'], string> = {
  thought: 'text-violet-600 bg-violet-50 border-violet-200',
  action: 'text-accent bg-accent/10 border-accent/20',
  observation: 'text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-500/15 border-emerald-200 dark:border-emerald-500/20',
};

const STEP_LABEL: Record<TimelineEvent['type'], string> = {
  thought: 'Thought',
  action: 'Action',
  observation: 'Observation',
};

/** Coalesce adjacent thought tokens into a single row for readability. */
function collapseThoughts(timeline: TimelineEvent[]): TimelineEvent[] {
  const out: TimelineEvent[] = [];
  for (const ev of timeline) {
    const last = out[out.length - 1];
    if (
      ev.type === 'thought'
      && last
      && last.type === 'thought'
      && last.step === ev.step
    ) {
      last.content = (last.content || '') + (ev.content || '');
      continue;
    }
    out.push({ ...ev });
  }
  return out;
}

function TimelineRow({ event, defaultOpen }: { event: TimelineEvent; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const Icon = STEP_ICON[event.type];
  const colorClass = event.is_error
    ? 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15 border-red-200 dark:border-red-500/20'
    : STEP_COLOR[event.type];

  const headerText = useMemo(() => {
    if (event.type === 'action') return event.tool || 'unknown_tool';
    if (event.type === 'observation') return event.is_error ? 'Error' : 'Result';
    return '';
  }, [event]);

  const body = event.type === 'thought'
    ? (event.content || '').trim()
    : event.type === 'action'
      ? (event.input_preview || '').trim()
      : (event.result_preview || '').trim();

  const collapsible = event.type !== 'thought' && body.length > 0;

  return (
    <div className="flex gap-3 pl-2">
      <div className="flex flex-col items-center">
        <div className={`flex items-center justify-center w-6 h-6 rounded-full border ${colorClass}`}>
          <Icon size={12} />
        </div>
        <div className="flex-1 w-px bg-muted mt-1" />
      </div>
      <div className="flex-1 pb-3 min-w-0">
        <button
          type="button"
          disabled={!collapsible}
          onClick={() => collapsible && setOpen(o => !o)}
          className={`flex items-center gap-1.5 text-xs font-medium ${collapsible ? 'hover:text-foreground cursor-pointer' : 'cursor-default'} text-foreground`}
        >
          {collapsible && (open ? <ChevronDown size={12} /> : <ChevronRight size={12} />)}
          <span className="uppercase tracking-wide text-[10px] text-muted-foreground">
            Step {event.step} · {STEP_LABEL[event.type]}
          </span>
          {headerText && (
            <span className="font-mono text-[11px] text-foreground truncate max-w-[24ch]">
              {headerText}
            </span>
          )}
          {event.is_error && <AlertCircle size={11} className="text-red-500" />}
        </button>
        {body && (event.type === 'thought' || open) && (
          <div className={`mt-1 text-xs leading-relaxed whitespace-pre-wrap break-words ${event.is_error ? 'text-red-700 dark:text-red-400' : 'text-foreground'}`}>
            {body}
          </div>
        )}
      </div>
    </div>
  );
}

export default function DecisionMakerTimeline({
  timeline, budget, isStreaming,
}: DecisionMakerTimelineProps) {
  const [showAll, setShowAll] = useState(false);
  const collapsed = collapseThoughts(timeline);
  const MAX_VISIBLE = 10;
  const hasOverflow = collapsed.length > MAX_VISIBLE;
  const visible = showAll || !hasOverflow
    ? collapsed
    : collapsed.slice(collapsed.length - MAX_VISIBLE);

  return (
    <div className="mt-2 mb-3 rounded-lg border border-violet-200 bg-gradient-to-br from-violet-50/60 to-white p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Sparkles size={14} className="text-violet-600" />
          <span className="text-xs font-semibold text-violet-900">
            Decision Maker Reasoning
          </span>
          {isStreaming && (
            <span className="text-[10px] text-violet-700 px-1.5 py-0.5 bg-violet-100 rounded-full border border-violet-200 animate-pulse">
              thinking…
            </span>
          )}
        </div>
        {budget && (
          <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <Clock size={10} />
            <span>
              step {budget.iteration}/{budget.max_iterations} · {budget.elapsed_s.toFixed(1)}s
            </span>
          </div>
        )}
      </div>

      {timeline.length === 0 ? (
        <div className="text-xs italic text-muted-foreground pl-1">
          Planning…
        </div>
      ) : (
        <>
          {hasOverflow && !showAll && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="text-[11px] text-violet-700 hover:text-violet-900 hover:underline mb-2"
            >
              Show {collapsed.length - MAX_VISIBLE} earlier step(s)
            </button>
          )}
          <div>
            {visible.map((ev, i) => (
              <TimelineRow
                key={`${ev.step}-${ev.type}-${i}`}
                event={ev}
                defaultOpen={i === visible.length - 1}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
