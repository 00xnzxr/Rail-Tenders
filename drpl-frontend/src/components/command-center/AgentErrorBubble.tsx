/**
 * AgentErrorBubble — renders typed agent_warning / agent_error SSE events
 * as distinct UI bubbles below the assistant message.
 *
 * Replaces the previous behaviour where errors were silently concatenated
 * into the agent's response text (via the JSON-parse fallback at
 * CommandCenterPage.tsx:884). Now: every error and warning the backend
 * emits comes through a typed channel and renders here with proper
 * styling + a "what does this mean?" tooltip per error code.
 *
 * Plan: ~/.claude/plans/now-i-need-to-synchronous-taco.md (Phase 3)
 */

import { AlertTriangle, Info } from 'lucide-react';
import type { AgentNotice } from '../../types/command-center';

interface AgentErrorBubbleProps {
  notice: AgentNotice;
}

/** Per-code human-friendly explanations. Falls back to the raw message. */
const CODE_EXPLANATIONS: Record<string, string> = {
  context_trimmed:
    "The request included more information than DRPL could read at once, so it focused on the bidding schedule and the main tender document.",
  persistence_failed:
    "The response is available, but its editable cost breakdown could not be saved. Trying the request again usually fixes this.",
  context_overflow:
    "These documents are too large to read together. Attach the main NIT PDF by itself and try again.",
  parse_failed:
    "DRPL could not finish organizing the response. Please try the request again.",
  tool_failed:
    "One supporting service was unavailable, so this result may be incomplete.",
  internal_error:
    "An unexpected backend error occurred. The platform team has been notified.",
};

export function AgentErrorBubble({ notice }: AgentErrorBubbleProps) {
  const isError = notice.severity === 'error';
  const Icon = isError ? AlertTriangle : Info;
  const explanation = CODE_EXPLANATIONS[notice.code];

  const containerCls = isError
    ? 'border-red-300 bg-red-50 dark:bg-red-500/15 text-red-900'
    : 'border-amber-300 bg-amber-50 dark:bg-amber-500/15 text-amber-900';
  const iconCls = isError ? 'text-red-600 dark:text-red-400' : 'text-amber-600 dark:text-amber-400';

  // Pretty-print trim_notes when present.
  const trimNotes: string[] = Array.isArray(notice.details?.trim_notes)
    ? notice.details.trim_notes
    : [];

  return (
    <div
      className={`mt-2 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${containerCls}`}
      role={isError ? 'alert' : 'status'}
    >
      <Icon size={14} className={`mt-0.5 flex-shrink-0 ${iconCls}`} />
      <div className="flex-1 space-y-1">
        <div className="font-semibold">{notice.message}</div>
        {explanation && (
          <div className="text-[11px] leading-relaxed opacity-90">
            {explanation}
          </div>
        )}
        {trimNotes.length > 0 && (
          <ul className="ml-4 list-disc space-y-0.5 text-[11px] opacity-80">
            {trimNotes.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

interface AgentNoticeListProps {
  notices: AgentNotice[] | undefined;
}

/** Convenience wrapper that renders an array of notices, sorting errors
 * above warnings so the user sees the most important issue first.
 */
export function AgentNoticeList({ notices }: AgentNoticeListProps) {
  if (!notices || notices.length === 0) return null;
  const sorted = [...notices].sort((a, b) => {
    if (a.severity === b.severity) return 0;
    return a.severity === 'error' ? -1 : 1;
  });
  return (
    <div className="space-y-1.5">
      {sorted.map((n, i) => (
        <AgentErrorBubble key={`${n.code}-${i}`} notice={n} />
      ))}
    </div>
  );
}

export default AgentErrorBubble;
