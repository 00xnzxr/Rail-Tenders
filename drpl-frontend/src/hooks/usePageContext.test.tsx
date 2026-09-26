import { describe, expect, it } from 'vitest'
import { renderHook } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { usePageContext } from './usePageContext'

/**
 * The context this hook derives is what lets the assistant answer "why is this
 * wrong?" without the user naming anything. If it silently stops picking up
 * the tender, the assistant degrades into vague, unhelpful answers rather than
 * failing visibly — so it is worth pinning.
 */
function at(path: string) {
  return renderHook(() => usePageContext(), {
    wrapper: ({ children }) => (
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="*" element={<>{children}</>} />
        </Routes>
      </MemoryRouter>
    ),
  })
}

describe('usePageContext', () => {
  it('always reports the current route', () => {
    const { result } = at('/admin/users')
    expect(result.current.route).toBe('/admin/users')
  })

  it('picks the tender id out of a tender route', () => {
    const { result } = at('/tenders/412')
    expect(result.current.tender_id).toBe(412)
  })

  it('picks up tender and tab on a workspace route', () => {
    const { result } = at('/tenders/412/workspace/checklist')
    expect(result.current.tender_id).toBe(412)
    expect(result.current.workspace_tab).toBe('checklist')
  })

  it('omits the tender on a page that has none', () => {
    const { result } = at('/dashboard')
    expect(result.current.tender_id).toBeUndefined()
    expect(result.current.workspace_tab).toBeUndefined()
  })

  it('does not mistake a non-numeric segment for a tender id', () => {
    const { result } = at('/tenders/archive')
    expect(result.current.tender_id).toBeUndefined()
  })

  it('handles a hyphenated workspace tab', () => {
    const { result } = at('/tenders/7/workspace/cost-breakdown')
    expect(result.current.workspace_tab).toBe('cost-breakdown')
  })
})
