import { describe, expect, it, vi, beforeEach } from 'vitest'
import { streamAssistantMessage, respondToPendingAction } from './assistant'

/**
 * The SSE parser is the piece most likely to break silently: a malformed frame
 * or a chunk split mid-line degrades into "the assistant said nothing" rather
 * than an error anyone notices. These tests feed it deliberately awkward
 * streams.
 */

function sseStream(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c))
      controller.close()
    },
  })
  return new Response(body, { status: 200 })
}

function collector() {
  const tokens: string[] = []
  const details: string[] = []
  const errors: string[] = []
  const actions: any[] = []
  return {
    tokens,
    details,
    errors,
    actions,
    handlers: {
      onToken: (c: string) => tokens.push(c),
      onTokenReset: () => tokens.splice(0, tokens.length),
      onDetail: (d: string) => details.push(d),
      onError: (e: string) => errors.push(e),
      onPendingAction: (a: any) => actions.push(a),
    },
  }
}

beforeEach(() => {
  localStorage.setItem('drpl_token', 'test-token')
  vi.restoreAllMocks()
})

describe('streamAssistantMessage', () => {
  it('collects tokens in order', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: token\ndata: {"content":"Hello "}\n\n',
          'event: token\ndata: {"content":"world"}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'hi', { route: '/x' }, c.handlers)

    expect(c.tokens.join('')).toBe('Hello world')
  })

  it('reassembles an event split across chunk boundaries', async () => {
    // The network splits wherever it likes; a frame cut mid-JSON must not be
    // dropped.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream(['event: token\ndata: {"cont', 'ent":"split"}\n\n']),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'hi', { route: '/x' }, c.handlers)

    expect(c.tokens.join('')).toBe('split')
  })

  it('accepts both the content and text token shapes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: token\ndata: {"content":"a"}\n\n',
          'event: token\ndata: {"text":"b"}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'hi', { route: '/x' }, c.handlers)

    expect(c.tokens.join('')).toBe('ab')
  })

  it('surfaces a pending action from confirm_required', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: confirm_required\ndata: {"tool":"regenerate_checklist","tier":"write","summary":"Rebuild it.","args":{}}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'go', { route: '/x' }, c.handlers)

    expect(c.actions).toHaveLength(1)
    expect(c.actions[0].tool).toBe('regenerate_checklist')
    expect(c.actions[0].tier).toBe('write')
  })

  it('also surfaces a pending action carried on agent_complete', async () => {
    // Needed for a page refresh / resumed stream, where the run finishes
    // rather than emitting the live confirm_required event.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: agent_complete\ndata: {"status":"needs_confirmation","pending_action":{"tool":"finalize_document","tier":"destructive","summary":"Final.","args":{}}}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'go', { route: '/x' }, c.handlers)

    expect(c.actions[0].tier).toBe('destructive')
  })

  it('routes trace events to detail, not to the visible answer', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: decision_thought\ndata: {"content":"considering"}\n\n',
          'event: decision_action\ndata: {"tool":"inspect_tender"}\n\n',
          'event: token\ndata: {"content":"Answer"}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'hi', { route: '/x' }, c.handlers)

    expect(c.tokens.join('')).toBe('Answer')
    expect(c.details.join(' ')).toContain('inspect_tender')
  })

  it('drops pre-tool narration on token_reset', async () => {
    // A tool-using agent says "Let me search for that" on its way to a tool
    // call. That text is not the answer and must not be saved with it.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: token\ndata: {"content":"Let me search for that."}\n\n',
          'event: token_reset\ndata: {}\n\n',
          'event: token\ndata: {"content":"The rate is Rs 75,940.72."}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'what is the rate?', { route: '/x' }, c.handlers)

    expect(c.tokens.join('')).toBe('The rate is Rs 75,940.72.')
  })

  it('ignores unparseable frames instead of throwing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          ': keepalive\n\n',
          'event: token\ndata: not-json\n\n',
          'event: token\ndata: {"content":"ok"}\n\n',
        ]),
      ),
    )
    const c = collector()

    await streamAssistantMessage(1, 'hi', { route: '/x' }, c.handlers)

    expect(c.tokens.join('')).toBe('ok')
    expect(c.errors).toHaveLength(0)
  })

  it('reports a non-2xx response as an error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('nope', { status: 500 })),
    )
    const c = collector()

    await streamAssistantMessage(1, 'hi', { route: '/x' }, c.handlers)

    expect(c.errors[0]).toContain('500')
  })

  it('sends the page context with the message', async () => {
    const fetchMock = vi.fn().mockResolvedValue(sseStream([]))
    vi.stubGlobal('fetch', fetchMock)

    await streamAssistantMessage(
      7,
      'why is this wrong?',
      { route: '/tenders/412/workspace/checklist', tender_id: 412 },
      collector().handlers,
    )

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.message).toBe('why is this wrong?')
    expect(body.page_context.tender_id).toBe(412)
    expect(fetchMock.mock.calls[0][0]).toContain('/sessions/7/chat/stream')
  })
})

describe('respondToPendingAction', () => {
  it('posts the decision to the action endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(sseStream([]))
    vi.stubGlobal('fetch', fetchMock)

    await respondToPendingAction(9, 'approve', collector().handlers)

    expect(fetchMock.mock.calls[0][0]).toContain('/sessions/9/action/respond')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).action).toBe('approve')
  })

  it('streams the follow-up answer after approval', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream(['event: token\ndata: {"content":"Done."}\n\n']),
      ),
    )
    const c = collector()

    await respondToPendingAction(9, 'approve', c.handlers)

    expect(c.tokens.join('')).toBe('Done.')
  })
})

describe('live progress', () => {
  it('surfaces agent_status as visible activity, not hidden detail', async () => {
    // The backend now sends a plain-language message per step; it must reach
    // onStatus (the activity card) rather than onDetail (collapsed technical
    // detail), or the user watches a spinner with no explanation.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: agent_status\ndata: {"phase":"tool_running","run_id":"r1","message":"Researching rates and building the costing"}\n\n',
        ]),
      ),
    )
    const c = collector()
    const statuses: any[] = []

    await streamAssistantMessage(1, 'cost it', { route: '/x' }, {
      ...c.handlers,
      onStatus: (s: any) => statuses.push(s),
    })

    expect(statuses).toHaveLength(1)
    expect(statuses[0].message).toBe('Researching rates and building the costing')
    expect(c.details).toHaveLength(0)
  })

  it('marks a finished step as completed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: agent_status\ndata: {"phase":"tool_done","run_id":"r1"}\n\n',
        ]),
      ),
    )
    const statuses: any[] = []

    await streamAssistantMessage(1, 'x', { route: '/x' }, {
      ...collector().handlers,
      onStatus: (s: any) => statuses.push(s),
    })

    expect(statuses[0].phase).toBe('completed')
  })

  it('never shows a raw tool name as the activity message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseStream([
          'event: agent_status\ndata: {"phase":"tool_running","message":"Extracting the annexures","tool":"call_annexure_finder"}\n\n',
        ]),
      ),
    )
    const statuses: any[] = []

    await streamAssistantMessage(1, 'x', { route: '/x' }, {
      ...collector().handlers,
      onStatus: (s: any) => statuses.push(s),
    })

    expect(statuses[0].message).not.toContain('call_')
    expect(statuses[0].message).not.toContain('_')
  })
})
