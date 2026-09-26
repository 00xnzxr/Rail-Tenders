/**
 * What one run cost, under the answer it paid for.
 *
 * The figure is derived from the ledger — `APIUsageLog` rows summed by
 * `run_id` — not from a counter kept alongside it. A run is the Master plus
 * every worker it delegated to plus every retry, so `calls` is usually more
 * than one and the total is the only number worth showing.
 */

export interface RunCost {
  run_id: string | null;
  calls: number;
  tokens_input: number;
  tokens_output: number;
  tokens_total: number;
  cost_usd: number;
}

/** Dollars at a precision that survives a Haiku run.
 *
 * Two decimal places print "$0.00" for most runs on this roster, which reads
 * as "free" rather than "small". Sub-cent amounts keep four.
 */
function formatUsd(value: number): string {
  if (value >= 0.01) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(4)}`;
}

function formatTokens(total: number): string {
  if (total >= 1_000_000) return `${(total / 1_000_000).toFixed(1)}M`;
  if (total >= 1_000) return `${(total / 1_000).toFixed(1)}k`;
  return `${total}`;
}

export function RunCostLine({ cost }: { cost?: RunCost | null }) {
  // A run whose calls never reached the ledger renders nothing. A stray
  // "$0.0000" would claim the run was free, which is a different statement
  // from "not recorded".
  if (!cost || !cost.calls) return null;

  return (
    <p className="mt-2 text-[10px] text-muted-foreground" title={`Run ${cost.run_id ?? ''}`}>
      {formatTokens(cost.tokens_total)} tokens · {formatUsd(cost.cost_usd)} ·{' '}
      {cost.calls} {cost.calls === 1 ? 'call' : 'calls'}
    </p>
  );
}
