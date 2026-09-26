import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import ConfirmActionCard from './ConfirmActionCard'

/**
 * This card is the last thing between an autonomous agent and the user's data,
 * and it is read by people who cannot evaluate what the agent is about to do.
 * Two things must hold: it says what will happen in plain words, and an
 * irreversible action does not look like an ordinary one.
 */
const write = {
  tool: 'regenerate_checklist',
  tier: 'write' as const,
  summary: 'Rebuild the submission checklist. The current checklist will be replaced.',
  args: {},
}

const destructive = {
  tool: 'init_workspace_force',
  tier: 'destructive' as const,
  summary: 'Reset this workspace and rebuild it from scratch.',
  args: {},
}

describe('ConfirmActionCard', () => {
  it('shows the plain-language summary', () => {
    render(<ConfirmActionCard action={write} onApprove={vi.fn()} onDeny={vi.fn()} />)
    expect(screen.getByText(/Rebuild the submission checklist/)).toBeTruthy()
  })

  it('never shows the raw tool name', () => {
    const { container } = render(
      <ConfirmActionCard action={write} onApprove={vi.fn()} onDeny={vi.fn()} />,
    )
    expect(container.textContent).not.toContain('regenerate_checklist')
  })

  it('distinguishes an irreversible action from an ordinary one', () => {
    const ordinary = render(
      <ConfirmActionCard action={write} onApprove={vi.fn()} onDeny={vi.fn()} />,
    )
    expect(ordinary.container.textContent).toContain('Can I go ahead?')

    const irreversible = render(
      <ConfirmActionCard action={destructive} onApprove={vi.fn()} onDeny={vi.fn()} />,
    )
    expect(irreversible.container.textContent).toContain('cannot be undone')
  })

  it('calls onApprove and onDeny', async () => {
    const onApprove = vi.fn()
    const onDeny = vi.fn()
    render(<ConfirmActionCard action={write} onApprove={onApprove} onDeny={onDeny} />)

    screen.getByRole('button', { name: /^Yes$/ }).click()
    screen.getByRole('button', { name: /No/ }).click()

    expect(onApprove).toHaveBeenCalledOnce()
    expect(onDeny).toHaveBeenCalledOnce()
  })

  it('disables both choices while a decision is in flight', () => {
    render(
      <ConfirmActionCard action={write} busy onApprove={vi.fn()} onDeny={vi.fn()} />,
    )
    for (const b of screen.getAllByRole('button')) {
      expect((b as HTMLButtonElement).disabled).toBe(true)
    }
  })
})
