import { useEffect, useRef, useState } from 'react';
import { HelpCircle, X, Loader2 } from 'lucide-react';
import type { PendingClarification } from '../../lib/api';

interface Props {
  open: boolean;
  clarification: PendingClarification | null;
  onCancel: () => void;
  onSubmit: (answer: string) => Promise<void> | void;
  submitting?: boolean;
}

export default function ClarificationModal({
  open,
  clarification,
  onCancel,
  onSubmit,
  submitting = false,
}: Props) {
  const [answer, setAnswer] = useState('');
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (open) {
      setAnswer('');
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open, clarification?.id]);

  if (!open || !clarification) return null;

  const reason = (clarification.context as any)?.reason as string | undefined;

  const handleSubmit = async () => {
    const trimmed = answer.trim();
    if (!trimmed || submitting) return;
    await onSubmit(trimmed);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-2xl bg-card shadow-2xl">
        <div className="flex items-start justify-between border-b border-border p-5">
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400">
              <HelpCircle size={18} />
            </div>
            <div>
              <div className="text-sm font-semibold text-foreground">
                Quick question from the agent
              </div>
              <div className="text-xs text-muted-foreground">
                {clarification.agent_key.replace(/_/g, ' ')}
              </div>
            </div>
          </div>
          <button
            className="text-muted-foreground transition hover:text-muted-foreground disabled:opacity-50"
            onClick={onCancel}
            disabled={submitting}
            aria-label="Dismiss"
          >
            <X size={18} />
          </button>
        </div>

        <div className="space-y-3 p-5">
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">
            {clarification.question}
          </p>

          {reason && (
            <p className="rounded-md bg-muted/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
              {reason}
            </p>
          )}

          {clarification.options?.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {clarification.options.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  className="rounded-full border border-border bg-card px-3 py-1 text-xs text-foreground transition hover:border-accent/40 hover:bg-muted/40"
                  onClick={() => setAnswer(opt)}
                  disabled={submitting}
                >
                  {opt}
                </button>
              ))}
            </div>
          )}

          <textarea
            ref={inputRef}
            className="h-24 w-full resize-none rounded-lg border border-border p-3 text-sm text-foreground focus:border-ring focus:outline-none"
            placeholder="Your answer..."
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSubmit();
              }
            }}
            disabled={submitting}
          />
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border p-4">
          <button
            className="rounded-lg px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted/40 disabled:opacity-50"
            onClick={onCancel}
            disabled={submitting}
          >
            Dismiss
          </button>
          <button
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={handleSubmit}
            disabled={submitting || !answer.trim()}
          >
            {submitting ? <Loader2 size={14} className="animate-spin" /> : null}
            Send answer
          </button>
        </div>
      </div>
    </div>
  );
}
