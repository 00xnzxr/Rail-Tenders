import { AlertTriangle, Check, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { PendingAction } from '@/lib/assistant'

/**
 * The confirmation the agent waits on before changing anything.
 *
 * Deliberately plain: the person reading it may not know what a "tool" is, so
 * it states what will happen in ordinary words and nothing else. Destructive
 * actions get a visibly different, heavier treatment — the difference between
 * "this can be redone" and "this cannot be undone" is the one thing the reader
 * must not miss.
 */
export default function ConfirmActionCard({
  action,
  busy,
  onApprove,
  onDeny,
}: {
  action: PendingAction
  busy?: boolean
  onApprove: () => void
  onDeny: () => void
}) {
  const destructive = action.tier === 'destructive'

  return (
    <div
      className={[
        'rounded-2xl border p-4 text-sm shadow-sm',
        destructive
          ? 'border-destructive/40 bg-destructive/5'
          : 'border-emerald-500/20 bg-emerald-500/5',
      ].join(' ')}
      role="alertdialog"
      aria-label="Confirm this action"
    >
      <div className="flex items-start gap-2">
        {destructive && (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden />
        )}
        <div className="min-w-0">
          <p className="font-medium text-foreground">
            {destructive ? 'This cannot be undone' : 'Can I go ahead?'}
          </p>
          <p className="mt-1 text-muted-foreground">{action.summary}</p>
        </div>
      </div>

      <p className="mt-3 text-[11px] font-semibold text-muted-foreground">
        {destructive ? 'Review this carefully before continuing.' : 'Nothing changes until you approve.'}
      </p>

      <div className="mt-3 flex gap-2">
        <Button size="sm" onClick={onApprove} disabled={busy}>
          <Check className="mr-1 h-3.5 w-3.5" aria-hidden />
          {destructive ? 'Yes, do it' : 'Yes'}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDeny} disabled={busy}>
          <X className="mr-1 h-3.5 w-3.5" aria-hidden />
          No
        </Button>
      </div>
    </div>
  )
}
