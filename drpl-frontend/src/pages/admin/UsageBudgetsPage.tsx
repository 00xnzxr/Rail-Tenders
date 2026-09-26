import { useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'
import Header from '../../components/layout/Header'
import LoadingSpinner from '../../components/ui/LoadingSpinner'
import { api } from '../../lib/api'
import { normalizeRole, ROLE_LABELS } from '@/lib/roles'

type UserUsage = {
  user_id: number
  email: string
  name: string
  role: string
  spend_usd: number
  limit_usd: number
  percent_used: number
  status: 'ok' | 'warning' | 'exceeded'
  override_active: boolean
  enforced: boolean
}

type RunRow = {
  run_id: string
  calls: number
  tokens_input: number
  tokens_output: number
  tokens_total: number
  cost_usd: number
  user_id: number | null
  email: string | null
  agent_name: string | null
  last_call_at: string | null
}

type Rollup = {
  period_start: string
  users: UserUsage[]
  total_spend_usd: number
  unattributed_spend_usd: number
}

const STATUS_STYLES: Record<UserUsage['status'], string> = {
  ok: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  warning: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-500',
  exceeded: 'bg-destructive/15 text-destructive',
}

const BAR_STYLES: Record<UserUsage['status'], string> = {
  ok: 'bg-emerald-500',
  warning: 'bg-amber-500',
  exceeded: 'bg-destructive',
}

export default function UsageBudgetsPage() {
  const [data, setData] = useState<Rollup | null>(null)
  const [runs, setRuns] = useState<RunRow[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = () => {
    setLoading(true)
    api.get<Rollup>('/api/admin/usage')
      .then((r) => { setData(r.data); setError(null) })
      .catch(() => setError('Could not load usage.'))
      .finally(() => setLoading(false))
    // Per-run rows are a separate call on purpose: the rollup above is the
    // thing that gates people, and it must still render if this one fails.
    api.get<{ runs: RunRow[] }>('/api/admin/usage/runs?limit=50')
      .then((r) => setRuns(r.data.runs))
      .catch(() => setRuns([]))
  }

  useEffect(load, [])

  const act = async (fn: () => Promise<unknown>, userId: number) => {
    setBusy(userId)
    try { await fn(); load() } finally { setBusy(null) }
  }

  const grantOverride = (u: UserUsage) =>
    act(() => api.post(`/api/admin/usage/${u.user_id}/override`, { days: 7 }), u.user_id)

  const revokeOverride = (u: UserUsage) =>
    act(() => api.delete(`/api/admin/usage/${u.user_id}/override`), u.user_id)

  const setLimit = (u: UserUsage) => {
    const entered = window.prompt(
      `Monthly limit for ${u.name} in USD.\nLeave blank to use the platform default.`,
      String(u.limit_usd),
    )
    if (entered === null) return
    const value = entered.trim() === '' ? null : Number(entered)
    if (value !== null && (!Number.isFinite(value) || value < 0)) return
    return act(
      () => api.put(`/api/admin/usage/${u.user_id}/limit`, { monthly_limit_usd: value }),
      u.user_id,
    )
  }

  if (loading && !data) return <LoadingSpinner />

  return (
    <div className="flex min-h-full flex-col">
      <Header title="Usage & budgets" />
      <div className="flex-1 space-y-4 p-6">
        {error && <p className="text-sm text-destructive">{error}</p>}

        {data && (
          <>
            <div className="flex flex-wrap items-center gap-4 rounded-xl border border-border bg-card p-4">
              <div>
                <p className="text-xs text-muted-foreground">Period from</p>
                <p className="text-sm font-medium">{data.period_start.slice(0, 10)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Total spend</p>
                <p className="text-sm font-medium">${data.total_spend_usd.toFixed(2)}</p>
              </div>
              <div>
                {/* Platform work with no human behind it — seeders, the archive
                    sweep, scheduled scoring. Shown rather than folded into a
                    user's figure or quietly dropped from the total. */}
                <p className="text-xs text-muted-foreground">Platform (unattributed)</p>
                <p className="text-sm font-medium">${data.unattributed_spend_usd.toFixed(2)}</p>
              </div>
              <button onClick={load}
                className="ml-auto flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted">
                <RefreshCw size={14} /> Refresh
              </button>
            </div>

            <div className="overflow-x-auto rounded-xl border border-border bg-card">
              <table className="w-full text-sm">
                <thead className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">User</th>
                    <th className="px-4 py-3">Role</th>
                    <th className="px-4 py-3">Used</th>
                    <th className="px-4 py-3 w-48">Share of limit</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.users.map((u) => (
                    <tr key={u.user_id} className="border-b border-border/60 last:border-0">
                      <td className="px-4 py-3">
                        <div className="font-medium">{u.name}</div>
                        <div className="text-xs text-muted-foreground">{u.email}</div>
                      </td>
                      <td className="px-4 py-3 text-xs">{ROLE_LABELS[normalizeRole(u.role)]}</td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        ${u.spend_usd.toFixed(2)}
                        <span className="text-muted-foreground"> / ${u.limit_usd.toFixed(2)}</span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                          <div className={`h-full rounded-full ${BAR_STYLES[u.status]}`}
                               style={{ width: `${u.percent_used}%` }} />
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[u.status]}`}>
                          {u.status}
                        </span>
                        {!u.enforced && (
                          <span className="ml-2 text-xs text-muted-foreground">not enforced</span>
                        )}
                        {u.override_active && (
                          <span className="ml-2 inline-flex items-center gap-1 text-xs text-muted-foreground">
                            <ShieldCheck size={12} /> override
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right whitespace-nowrap">
                        <button onClick={() => setLimit(u)} disabled={busy === u.user_id}
                          className="rounded-lg border border-border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50">
                          Set limit
                        </button>
                        {u.override_active ? (
                          <button onClick={() => revokeOverride(u)} disabled={busy === u.user_id}
                            className="ml-2 rounded-lg border border-border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50">
                            Revoke
                          </button>
                        ) : (
                          <button onClick={() => grantOverride(u)} disabled={busy === u.user_id}
                            className="ml-2 rounded-lg border border-border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50">
                            Allow 7 days
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="rounded-xl border border-border bg-card">
              <div className="flex items-baseline justify-between border-b border-border px-4 py-3">
                <h2 className="text-sm font-semibold">Recent runs</h2>
                <p className="text-xs text-muted-foreground">
                  One row per agent run — the Master plus every worker it delegated to.
                </p>
              </div>
              {runs.length === 0 ? (
                <p className="px-4 py-6 text-xs text-muted-foreground">
                  No runs recorded yet. Runs started before per-run accounting shipped
                  have no run id and are not listed here; their spend is still counted
                  in the totals above.
                </p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-muted-foreground">
                      <th className="px-4 py-2 font-medium">When</th>
                      <th className="px-4 py-2 font-medium">User</th>
                      <th className="px-4 py-2 font-medium">Agent</th>
                      <th className="px-4 py-2 font-medium text-right">Calls</th>
                      <th className="px-4 py-2 font-medium text-right">Tokens</th>
                      <th className="px-4 py-2 font-medium text-right">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.run_id} className="border-t border-border">
                        <td className="px-4 py-2 text-xs text-muted-foreground">
                          {r.last_call_at ? r.last_call_at.slice(0, 16).replace('T', ' ') : '—'}
                        </td>
                        <td className="px-4 py-2 text-xs">{r.email ?? 'unattributed'}</td>
                        <td className="px-4 py-2 text-xs">{r.agent_name ?? '—'}</td>
                        <td className="px-4 py-2 text-right text-xs tabular-nums">{r.calls}</td>
                        <td className="px-4 py-2 text-right text-xs tabular-nums">
                          {r.tokens_total.toLocaleString()}
                        </td>
                        <td className="px-4 py-2 text-right text-xs tabular-nums">
                          ${r.cost_usd >= 0.01 ? r.cost_usd.toFixed(3) : r.cost_usd.toFixed(4)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {data.users.some((u) => u.status === 'exceeded' && !u.override_active && u.enforced) && (
              <p className="flex items-start gap-2 text-xs text-muted-foreground">
                <AlertTriangle size={14} className="mt-0.5 shrink-0 text-destructive" />
                Users at their limit cannot start new agent work until you grant an
                override or raise their limit. They can still read everything they
                have already produced.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
