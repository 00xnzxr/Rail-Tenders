/**
 * What a run cost, under the answer it paid for.
 *
 * The number comes from the ledger (`APIUsageLog` summed by `run_id`), sent on
 * the `done` event and also stored on the saved message so it survives a
 * reload. The component's job is to render it in a form a person can read:
 * dollars at a useful precision, tokens abbreviated.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RunCostLine } from './RunCostLine';

describe('RunCostLine', () => {
  it('shows the cost and the token total', () => {
    render(<RunCostLine cost={{ run_id: 'r1', calls: 4, tokens_input: 12000, tokens_output: 2400, tokens_total: 14400, cost_usd: 0.0312 }} />);

    expect(screen.getByText(/\$0\.031/)).toBeTruthy();
    expect(screen.getByText(/14\.4k tokens/)).toBeTruthy();
  });

  it('renders nothing when no cost was recorded', () => {
    // A run whose calls never reached the ledger must not leave a stray "$0.00"
    // under the answer, which reads as "this was free" rather than "unknown".
    const { container } = render(<RunCostLine cost={undefined} />);
    expect(container.innerHTML).toBe('');
  });

  it('renders nothing for a run that made no calls', () => {
    const { container } = render(
      <RunCostLine cost={{ run_id: 'r2', calls: 0, tokens_input: 0, tokens_output: 0, tokens_total: 0, cost_usd: 0 }} />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('keeps a sub-cent run legible instead of rounding it to zero', () => {
    // Haiku and Luna runs are frequently under a cent. Two decimal places would
    // print "$0.00" for every one of them and make the whole feature useless.
    render(<RunCostLine cost={{ run_id: 'r3', calls: 1, tokens_input: 900, tokens_output: 120, tokens_total: 1020, cost_usd: 0.0015 }} />);

    expect(screen.getByText(/\$0\.0015/)).toBeTruthy();
  });

  it('counts the calls a run made', () => {
    render(<RunCostLine cost={{ run_id: 'r4', calls: 7, tokens_input: 100, tokens_output: 100, tokens_total: 200, cost_usd: 0.01 }} />);

    expect(screen.getByText(/7 calls/)).toBeTruthy();
  });
});
