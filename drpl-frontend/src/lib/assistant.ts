import { api, API_BASE } from './api'
import type { PageContext } from '../hooks/usePageContext'

export interface AssistantSession {
  session_id: number
  router_session_id: string
  title: string
  preview?: string | null
  message_count?: number
  created_at?: string | null
  updated_at?: string | null
}

/** A write the agent wants to perform, waiting on the user's yes. */
export interface PendingAction {
  tool: string
  tier: 'write' | 'destructive'
  summary: string
  args: Record<string, unknown>
}

export interface AssistantMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  createdAt?: string
  /** Tool trace and agent reasoning, hidden behind "Show technical detail". */
  detail?: string
  pendingAction?: PendingAction
}

export interface AssistantStatus {
  phase: 'starting' | 'working' | 'completed'
  message: string
  detail?: string
  completed?: number
  total?: number
}

/** Resolve (creating on first use) the caller's persistent assistant thread. */
export async function getAssistantSession(): Promise<AssistantSession> {
  const { data } = await api.get<AssistantSession>('/api/command-center/assistant/session')
  return data
}

/** Start a separate chat while retaining all previous conversations. */
export async function createAssistantSession(): Promise<AssistantSession> {
  const { data } = await api.post<AssistantSession>('/api/command-center/assistant/sessions')
  return data
}

/** List saved pop-up conversations, newest first. */
export async function getAssistantSessions(): Promise<AssistantSession[]> {
  const { data } = await api.get<AssistantSession[]>('/api/command-center/assistant/sessions')
  return data
}

/** Load the durable conversation behind the floating assistant. */
export async function getAssistantHistory(sessionId: number): Promise<AssistantMessage[]> {
  const { data } = await api.get<any[]>(
    `/api/command-center/sessions/${sessionId}/history?limit=200`,
  )
  return data
    .filter((message) => message.role === 'user' || message.role === 'assistant')
    .map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content || '',
      createdAt: message.created_at,
      detail: message.metadata?.decision_trace
        ? JSON.stringify(message.metadata.decision_trace, null, 2)
        : undefined,
    }))
}

export interface StreamHandlers {
  onToken: (chunk: string) => void
  /**
   * Discard everything accumulated from `onToken` so far. A tool-using agent
   * narrates on its way to a tool call ("Let me search for that"); that text
   * is not part of the answer and must not be saved with it.
   */
  onTokenReset?: () => void
  onAgent?: (name: string) => void
  onDetail?: (line: string) => void
  onPendingAction?: (action: PendingAction) => void
  onStatus?: (status: AssistantStatus) => void
  onError?: (message: string) => void
}

/**
 * Read an SSE response body and dispatch its events.
 *
 * Shared by the chat and confirm-response calls because the backend returns
 * the identical event format for both — that was a deliberate choice on the
 * server so the client needs exactly one parser.
 */
async function consumeStream(response: Response, handlers: StreamHandlers): Promise<void> {
  const reader = response.body?.getReader()
  if (!reader) return

  const decoder = new TextDecoder()
  let buffer = ''
  let eventType = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    // Keep the trailing partial line for the next chunk.
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7).trim()
        continue
      }
      if (!line.startsWith('data: ')) continue

      let data: any
      try {
        data = JSON.parse(line.slice(6))
      } catch {
        continue // keepalive comments and split frames
      }

      switch (eventType) {
        case 'token':
          // The command-center stream uses `content`; other agent streams use
          // `text`. Accept both so this parser works against either.
          handlers.onToken(data.content ?? data.text ?? '')
          break
        case 'token_reset':
          handlers.onTokenReset?.()
          break
        case 'agent_start':
          handlers.onAgent?.(data.agent_name || data.display_name || data.agent_key || 'Assistant')
          handlers.onStatus?.({
            phase: 'working',
            message: `Working with ${data.agent_name || data.display_name || 'the right specialist'}`,
          })
          break
        case 'routing': {
          const names = data.agent_names || data.agents || []
          handlers.onStatus?.({
            phase: 'working',
            message: names.length
              ? `Planning the best route with ${names.join(', ')}`
              : 'Understanding what you need',
            detail: data.reasoning,
          })
          break
        }
        case 'agent_status':
          handlers.onStatus?.({
            phase: data.phase === 'tool_done' ? 'completed' : 'working',
            message: data.message || (data.phase === 'tool_done' ? 'Finished a work step' : 'Working on your request'),
            detail: data.tool,
          })
          break
        case 'analyzer_progress':
          handlers.onStatus?.({
            phase: data.phase === 'complete' ? 'completed' : 'working',
            message:
              data.phase === 'per_doc_complete'
                ? `Reading tender documents (${data.completed || 0} of ${data.total || 0})`
                : data.phase === 'synthesis_start'
                  ? 'Comparing requirements and preparing findings'
                  : data.phase === 'complete'
                    ? 'Tender analysis complete'
                    : 'Reading tender documents',
            completed: data.completed,
            total: data.total,
          })
          break
        case 'costing_batch_progress':
          handlers.onStatus?.({
            phase: 'working',
            message: data.batch
              ? `Estimating costs (${data.batch} of ${data.total_batches || data.totalBatches || 0} batches)`
              : 'Preparing the cost estimate',
            completed: data.lines_done || data.linesDone,
            total: data.lines_total || data.linesTotal,
          })
          break
        case 'decision_thought':
          if (data.content) handlers.onDetail?.(`Thinking: ${data.content}`)
          break
        case 'decision_action':
          if (data.tool) handlers.onDetail?.(`Using: ${data.tool}`)
          break
        case 'decision_observation':
          if (data.result_preview) handlers.onDetail?.(`Result: ${data.result_preview}`)
          break
        case 'confirm_required':
          handlers.onPendingAction?.(data as PendingAction)
          break
        case 'agent_complete':
          handlers.onStatus?.({ phase: 'completed', message: 'Finished this part of the work' })
          if (data.pending_action) {
            handlers.onPendingAction?.({
              ...(data.pending_action as PendingAction),
            })
          }
          break
        case 'error':
          handlers.onError?.(data.message || 'Something went wrong.')
          break
      }
    }
  }
}

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('drpl_token')
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

export async function streamAssistantMessage(
  sessionId: number,
  message: string,
  pageContext: PageContext,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/command-center/sessions/${sessionId}/chat/stream`,
    {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ message, page_context: pageContext }),
      signal,
    },
  )
  if (!response.ok) {
    handlers.onError?.(`Request failed (${response.status}).`)
    return
  }
  await consumeStream(response, handlers)
}

/** Approve or deny the write the agent is waiting on. */
export async function respondToPendingAction(
  sessionId: number,
  action: 'approve' | 'deny',
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/command-center/sessions/${sessionId}/action/respond`,
    {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ action }),
      signal,
    },
  )
  if (!response.ok) {
    handlers.onError?.(`Could not send your answer (${response.status}).`)
    return
  }
  await consumeStream(response, handlers)
}
