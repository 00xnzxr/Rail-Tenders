import { useEffect, useState, useCallback } from 'react'
import { AlertTriangle } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

export type Usage = {
  spend_usd: number
  limit_usd: number
  percent_used: number
  status: 'ok' | 'warning' | 'exceeded'
  override_active: boolean
  enforced: boolean
  period_start: string
}

/** Fires when a run finishes, so the bar reflects spend without a reload. */
export const USAGE_REFRESH_EVENT = 'drpl:usage-refresh'
export function refreshUsage() {
  window.dispatchEvent(new Event(USAGE_REFRESH_EVENT))
}

export function useUsage() {
  const [usage, setUsage] = useState<Usage | null>(null)

  const load = useCallback(() => {
    api.get<Usage>('/api/usage/me')
      .then((r) => setUsage(r.data))
      // A metering failure must never break the shell — the bar just hides.
      .catch(() => setUsage(null))
  }, [])

  useEffect(() => {
    load()
    window.addEventListener(USAGE_REFRESH_EVENT, load)
    return () => window.removeEventListener(USAGE_REFRESH_EVENT, load)
  }, [load])

  return usage
}

/**
 * Remaining share of this month's AI budget.
 *
 * Shows *remaining* rather than used: the number a user acts on is how much
 * they have left, and a bar that fills up as you work reads as progress toward
 * something good rather than toward being cut off.
 */
export default function UsageBar({ collapsed = false }: { collapsed?: boolean }) {
  const usage = useUsage()
  if (!usage) return null

  const remaining = Math.max(0, 100 - usage.percent_used)
  const blocked = usage.status === 'exceeded' && usage.enforced && !usage.override_active

  const tone =
    usage.status === 'exceeded'
      ? { bar: 'bg-destructive', text: 'text-destructive' }
      : usage.status === 'warning'
        ? { bar: 'bg-amber-500', text: 'text-amber-600 dark:text-amber-500' }
        : { bar: 'bg-emerald-500', text: 'text-muted-foreground' }

  const title =
    `$${usage.spend_usd.toFixed(2)} of $${usage.limit_usd.toFixed(2)} used this month` +
    (usage.override_active ? ' · admin override active' : '')

  if (collapsed) {
    return (
      <div className="px-3 py-2" title={title} aria-label={title}>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div className={cn('h-full rounded-full transition-all', tone.bar)} style={{ width: `${remaining}%` }} />
        </div>
      </div>
    )
  }

  return (
    <div className="px-3 py-2" title={title}>
      <div className="mb-1 flex items-center justify-between text-[11px] font-medium">
        <span className="text-muted-foreground">Monthly usage</span>
        <span className={tone.text}>{remaining.toFixed(0)}% left</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="progressbar"
           aria-valuenow={Math.round(remaining)} aria-valuemin={0} aria-valuemax={100}
           aria-label="Monthly AI budget remaining">
        <div className={cn('h-full rounded-full transition-all', tone.bar)} style={{ width: `${remaining}%` }} />
      </div>
      {blocked && (
        <p className="mt-1.5 flex items-start gap-1 text-[11px] leading-snug text-destructive">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          <span>Budget used up. Contact your administrator to continue this month.</span>
        </p>
      )}
      {usage.override_active && (
        <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
          Admin override active.
        </p>
      )}
    </div>
  )
}
