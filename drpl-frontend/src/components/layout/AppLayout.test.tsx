import { describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import { render, screen, act } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import AppLayout from './AppLayout'

/**
 * The blank-screen bug.
 *
 * The Command Center creates a session lazily on the first message and then
 * syncs the URL — `/command-center` → `/command-center/300` — while the send is
 * still in flight. AppLayout keyed the routed subtree on the raw pathname, so
 * that navigation unmounted and remounted the whole page: the optimistic
 * message, the streaming state and the in-flight request all went with it. The
 * user saw the empty task-picker instantly while the run carried on
 * server-side.
 *
 * These tests assert the page survives that navigation, and that genuinely
 * different routes still get a fresh mount.
 */

vi.mock('./Sidebar', () => ({
  default: () => <div data-testid="sidebar" />,
  useSidebarCollapsed: () => ({ collapsed: false, toggle: () => {} }),
}))
vi.mock('./SidebarNav', () => ({ default: () => <div /> }))
vi.mock('@/components/assistant/GlobalAssistant', () => ({ default: () => <div /> }))
vi.mock('@/components/ui/sheet', () => ({
  Sheet: ({ children }: any) => <div>{children}</div>,
  SheetContent: ({ children }: any) => <div>{children}</div>,
}))

/** Counts how many times it mounts, and exposes a way to navigate. */
function MountCounter({ mounts }: { mounts: { count: number } }) {
  const navigate = useNavigate()
  useEffect(() => {
    mounts.count += 1
  }, [mounts])
  return (
    <>
      <button onClick={() => navigate('/command-center/300', { replace: true })}>
        sync url
      </button>
      <button onClick={() => navigate('/tenders')}>go elsewhere</button>
    </>
  )
}

function renderAt(path: string, mounts: { count: number }) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/command-center" element={<MountCounter mounts={mounts} />} />
          <Route path="/command-center/:sessionId" element={<MountCounter mounts={mounts} />} />
          <Route path="/tenders" element={<MountCounter mounts={mounts} />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('AppLayout page identity', () => {
  it('does not remount the page when the composer syncs the session id into the URL', () => {
    const mounts = { count: 0 }
    renderAt('/command-center', mounts)
    expect(mounts.count).toBe(1)

    // Exactly what sendMessage does after creating the session mid-send.
    act(() => {
      screen.getByText('sync url').click()
    })

    expect(mounts.count).toBe(1)
  })

  it('still gives a different route its own mount', () => {
    const mounts = { count: 0 }
    renderAt('/command-center', mounts)
    expect(mounts.count).toBe(1)

    act(() => {
      screen.getByText('go elsewhere').click()
    })

    expect(mounts.count).toBe(2)
  })
})
