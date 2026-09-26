import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ThemeProvider } from '@/context/ThemeContext'
import GlobalAssistant from './GlobalAssistant'

/**
 * The panel opened and sat on "Loading your conversation…" forever, with the
 * send button spinning, while both endpoints returned 200 in milliseconds.
 *
 * The cause was a React race, not a network failure: the effect depended on
 * `sessionId` and set `sessionId` from inside its own async body. The state
 * change re-ran the effect, whose cleanup marked the still-in-flight first run
 * cancelled, so its `finally` skipped `setHistoryLoading(false)` — and the
 * second run returned early because `sessionId` was no longer null. Nothing
 * ever cleared the flag.
 *
 * These tests assert the panel reaches a usable state, which is what actually
 * broke, rather than asserting the shape of the fix.
 */

const SESSION = { session_id: 293, router_session_id: 'global-1-abc', title: 'Assistant' }

const HISTORY = [
  { id: 1, role: 'user' as const, content: 'Hi' },
  { id: 2, role: 'assistant' as const, content: 'Hello!' },
]

// These go through axios, not fetch, so mock at the module boundary. Mocking
// `fetch` here silently mocked nothing and the tests passed for the wrong
// reason — the unmocked calls simply rejected and hit the error path.
const getAssistantSession = vi.fn()
const getAssistantHistory = vi.fn()
const getAssistantSessions = vi.fn()
const createAssistantSession = vi.fn()

vi.mock('@/lib/assistant', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/assistant')>()
  return {
    ...actual,
    getAssistantSession: (...args: any[]) => getAssistantSession(...args),
    getAssistantHistory: (...args: any[]) => getAssistantHistory(...args),
    getAssistantSessions: (...args: any[]) => getAssistantSessions(...args),
    createAssistantSession: (...args: any[]) => createAssistantSession(...args),
    streamAssistantMessage: vi.fn().mockResolvedValue(undefined),
    respondToPendingAction: vi.fn().mockResolvedValue(undefined),
  }
})

function mockApi({
  historyDelayMs = 0,
  historyFails = false,
  sessionFails = false,
}: { historyDelayMs?: number; historyFails?: boolean; sessionFails?: boolean } = {}) {
  getAssistantSession.mockImplementation(async () => {
    if (sessionFails) throw new Error('session unavailable')
    return SESSION
  })
  getAssistantHistory.mockImplementation(async () => {
    if (historyDelayMs) await new Promise((r) => setTimeout(r, historyDelayMs))
    if (historyFails) throw new Error('history unavailable')
    return HISTORY
  })
  getAssistantSessions.mockResolvedValue([{ ...SESSION, message_count: HISTORY.length }])
  createAssistantSession.mockResolvedValue({
    session_id: 294,
    router_session_id: 'global-1-new',
    title: 'New conversation',
    message_count: 0,
  })
}

function renderPanel() {
  // MarkdownMessage reads the theme, so the panel cannot render without the
  // provider — without it the assistant's replies never mount at all.
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={['/admin/settings']}>
        <GlobalAssistant />
      </MemoryRouter>
    </ThemeProvider>,
  )
}

/** Open the panel the way a user does. */
function openPanel() {
  screen.getByRole('button', { name: /assistant/i }).click()
}

beforeEach(() => {
  localStorage.setItem('drpl_token', 'test-token')
  getAssistantSession.mockReset()
  getAssistantHistory.mockReset()
  getAssistantSessions.mockReset()
  createAssistantSession.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('GlobalAssistant', () => {
  it('stops loading once the conversation arrives', async () => {
    // Delay so the spinner is genuinely observable; with instant mocks the
    // load finishes before the first assertion and the test proves nothing.
    mockApi({ historyDelayMs: 30 })
    renderPanel()
    openPanel()

    expect(await screen.findByText(/Loading your conversation/i)).toBeTruthy()

    await waitFor(() => {
      expect(screen.queryByText(/Loading your conversation/i)).toBeNull()
    })
  })

  it('renders the loaded conversation', async () => {
    mockApi()
    renderPanel()
    openPanel()

    expect(await screen.findByText('Hello!')).toBeTruthy()
  })

  it('does not get stuck when history resolves after the session state settles', async () => {
    // The exact shape of the original bug: `setSessionId` lands, re-rendering
    // the component, while the history request is still in flight.
    mockApi({ historyDelayMs: 50 })
    renderPanel()
    openPanel()

    await waitFor(
      () => {
        expect(screen.queryByText(/Loading your conversation/i)).toBeNull()
      },
      { timeout: 3000 },
    )
  })

  it('still becomes usable when history fails', async () => {
    // A history failure must not block sending a new message.
    mockApi({ historyFails: true })
    renderPanel()
    openPanel()

    await waitFor(() => {
      expect(screen.queryByText(/Loading your conversation/i)).toBeNull()
    })
    expect(screen.getByLabelText(/Message the assistant|Ask anything/i)).toBeTruthy()
  })

  it('reports an unavailable assistant instead of spinning', async () => {
    mockApi({ sessionFails: true })
    renderPanel()
    openPanel()

    expect(await screen.findByText(/unavailable right now/i)).toBeTruthy()
    expect(screen.queryByText(/Loading your conversation/i)).toBeNull()
  })

  it('only resolves the session once per open', async () => {
    mockApi()
    renderPanel()
    openPanel()

    await waitFor(() => {
      expect(screen.queryByText(/Loading your conversation/i)).toBeNull()
    })

    expect(getAssistantSession).toHaveBeenCalledTimes(1)
  })

  it('starts a fresh conversation without removing saved chats', async () => {
    mockApi()
    renderPanel()
    openPanel()

    expect(await screen.findByText('Hello!')).toBeTruthy()
    screen.getByRole('button', { name: /new chat/i }).click()

    await waitFor(() => expect(createAssistantSession).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/what would you like to move forward/i)).toBeTruthy()
    expect(screen.queryByText('Hello!')).toBeNull()
  })

  it('shows separate saved conversations in history', async () => {
    mockApi()
    getAssistantSessions.mockResolvedValue([
      { ...SESSION, title: 'Eligibility for bridge tender', message_count: 4 },
      { ...SESSION, session_id: 292, title: 'Draft vendor email', message_count: 2 },
    ])
    renderPanel()
    openPanel()

    await screen.findByText('Hello!')
    screen.getByRole('button', { name: /^history$/i }).click()

    expect(await screen.findByText('Eligibility for bridge tender')).toBeTruthy()
    expect(screen.getByText('Draft vendor email')).toBeTruthy()
  })
})
