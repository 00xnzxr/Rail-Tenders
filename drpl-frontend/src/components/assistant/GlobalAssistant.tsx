import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  ArrowRight,
  Check,
  ChevronDown,
  Clock3,
  ExternalLink,
  FileCheck2,
  History,
  Loader2,
  MessageSquareText,
  Plus,
  ScanSearch,
  Send,
  Sparkles,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { MarkdownMessage } from '@/components/command-center/chat/MarkdownMessage'
import ConfirmActionCard from './ConfirmActionCard'
import { usePageContext } from '@/hooks/usePageContext'
import {
  createAssistantSession,
  getAssistantHistory,
  getAssistantSession,
  getAssistantSessions,
  respondToPendingAction,
  streamAssistantMessage,
  type AssistantMessage,
  type AssistantSession,
  type AssistantStatus,
  type PendingAction,
  type StreamHandlers,
} from '@/lib/assistant'

type ActivityStep = {
  label: string
  state: 'active' | 'completed'
  detail?: string
  completed?: number
  total?: number
}

const QUICK_TASKS = [
  { label: 'Find tenders we can bid on', prompt: 'Check current tenders and find the strongest opportunities we are eligible to bid on.', icon: ScanSearch },
  { label: 'Check eligibility', prompt: 'Check our eligibility for the tender on this page and clearly explain any gaps.', icon: FileCheck2 },
  { label: 'Explain how to proceed', prompt: 'Explain the best next steps for what I am viewing on this page.', icon: ArrowRight },
  { label: 'Prepare a checklist', prompt: 'Prepare a practical checklist for the tender on this page.', icon: Check },
] as const

/** Platform-wide assistant with saved conversations shared with Command Center. */
export default function GlobalAssistant() {
  const [open, setOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const [sessionId, setSessionId] = useState<number | null>(null)
  const [conversations, setConversations] = useState<AssistantSession[]>([])
  const [messages, setMessages] = useState<AssistantMessage[]>([])
  const [input, setInput] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [busy, setBusy] = useState(false)
  const [activity, setActivity] = useState<AssistantStatus | null>(null)
  const [activitySteps, setActivitySteps] = useState<ActivityStep[]>([])
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)

  const pageContext = usePageContext()
  const navigate = useNavigate()
  const location = useLocation()
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const detailRef = useRef<string[]>([])
  const textRef = useRef('')

  const contextLabel = useMemo(() => {
    if (pageContext.tender_id) return `Tender #${pageContext.tender_id}`
    if (pageContext.route === '/') return 'Home'
    const firstPart = pageContext.route.split('/').filter(Boolean)[0]
    return firstPart ? firstPart.replace(/-/g, ' ').replace(/^./, (c) => c.toUpperCase()) : 'DRPL'
  }, [pageContext])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setOpen((value) => !value)
      }
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Resolve the thread and load its history, once per mount.
  //
  // This effect must NOT depend on `sessionId`, because it sets `sessionId`
  // from inside its own async body. When it did, the state change re-ran the
  // effect, whose cleanup set `cancelled = true` for the still-in-flight first
  // run; the awaited history then resolved into a cancelled closure and the
  // `finally` skipped `setHistoryLoading(false)`. The second run returned
  // early at `sessionId !== null`, so nothing ever cleared the flag and the
  // panel sat on "Loading your conversation…" forever.
  //
  // A ref guards against duplicate loads instead of the state value, so the
  // effect's identity no longer changes as a result of its own work.
  const loadStartedRef = useRef(false)
  const newConversationRef = useRef(false)

  useEffect(() => {
    if (!open || loadStartedRef.current) return
    loadStartedRef.current = true

    let cancelled = false
    setHistoryLoading(true)

    ;(async () => {
      try {
        const session = await getAssistantSession()
        if (cancelled) return
        setSessionId(session.session_id)
        try {
          const [historyResult, conversationsResult] = await Promise.allSettled([
            getAssistantHistory(session.session_id),
            getAssistantSessions(),
          ])
          if (!cancelled) {
            if (historyResult.status === 'fulfilled') {
              setMessages(historyResult.value)
              setHistoryLoaded(true)
            }
            if (conversationsResult.status === 'fulfilled') {
              setConversations(conversationsResult.value)
            }
          }
        } catch {
          // A history failure must not block sending a new message.
        }
      } catch {
        if (!cancelled) {
          setSessionError('The assistant is unavailable right now.')
          // Allow a retry on the next open rather than wedging the panel.
          loadStartedRef.current = false
        }
      } finally {
        // Unconditional: the spinner is UI state for this mount, not something
        // a superseded run should be able to leave stuck on.
        setHistoryLoading(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [open])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, streamingText, pendingAction, activitySteps, historyOpen])

  useEffect(() => () => abortRef.current?.abort(), [])

  const recordStatus = useCallback((status: AssistantStatus) => {
    setActivity(status)
    setActivitySteps((previous) => {
      const next = previous.map((step) =>
        step.state === 'active' ? { ...step, state: 'completed' as const } : step,
      )
      const last = previous[previous.length - 1]
      const current = {
        label: status.message,
        state: status.phase === 'completed' ? 'completed' as const : 'active' as const,
        detail: status.detail,
        completed: status.completed,
        total: status.total,
      }
      if (last?.label === status.message) {
        next[next.length - 1] = current
        return next
      }
      return [...next, current].slice(-4)
    })
  }, [])

  const buildHandlers = useCallback((): StreamHandlers => {
    textRef.current = ''
    detailRef.current = []
    const push = (chunk: string) => {
      textRef.current += chunk
      setStreamingText(textRef.current)
    }
    return {
      onToken: push,
      onDetail: (line) => detailRef.current.push(line),
      onStatus: recordStatus,
      onPendingAction: (action) => setPendingAction(action),
      onError: (message) => push(`\n\n${message}`),
    }
  }, [recordStatus])

  const finalize = useCallback(() => {
    const text = textRef.current
    if (text.trim()) {
      const detail = detailRef.current.join('\n')
      setMessages((previous) => [
        ...previous,
        { role: 'assistant', content: text, detail: detail || undefined, createdAt: new Date().toISOString() },
      ])
    }
    textRef.current = ''
    setStreamingText('')
    setBusy(false)
    setActivity(null)
    setActivitySteps((previous) => previous.map((step) => ({ ...step, state: 'completed' as const })))
  }, [])

  const send = useCallback(async (prompt?: string) => {
    const message = (prompt ?? input).trim()
    if (!message || !sessionId || busy) return

    setInput('')
    setHistoryOpen(false)
    setPendingAction(null)
    setMessages((previous) => [...previous, { role: 'user', content: message, createdAt: new Date().toISOString() }])
    setConversations((previous) => previous.map((conversation) =>
      conversation.session_id === sessionId
        ? {
            ...conversation,
            title: conversation.title === 'New conversation' ? `${message.slice(0, 72)}${message.length > 72 ? '…' : ''}` : conversation.title,
            preview: conversation.preview || message,
            message_count: (conversation.message_count || 0) + 1,
          }
        : conversation,
    ))
    setBusy(true)
    setActivity({ phase: 'starting', message: 'Understanding what you need' })
    setActivitySteps([{ label: 'Understanding what you need', state: 'active' }])

    const controller = new AbortController()
    abortRef.current = controller
    try {
      await streamAssistantMessage(sessionId, message, pageContext, buildHandlers(), controller.signal)
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        textRef.current += '\n\nThe connection dropped. Please try again.'
        setStreamingText(textRef.current)
      }
    } finally {
      finalize()
    }
  }, [input, sessionId, busy, pageContext, buildHandlers, finalize])

  const startNewConversation = useCallback(async () => {
    if (busy || historyLoading || newConversationRef.current) return
    newConversationRef.current = true
    setHistoryLoading(true)
    setSessionError(null)
    try {
      const session = await createAssistantSession()
      setSessionId(session.session_id)
      setConversations((previous) => [session, ...previous])
      setMessages([])
      setInput('')
      setStreamingText('')
      setPendingAction(null)
      setActivity(null)
      setActivitySteps([])
      setHistoryOpen(false)
      setHistoryLoaded(true)
    } catch {
      setSessionError('Could not start a new conversation right now.')
    } finally {
      newConversationRef.current = false
      setHistoryLoading(false)
    }
  }, [busy, historyLoading])

  const selectConversation = useCallback(async (nextSessionId: number) => {
    if (busy) return
    if (nextSessionId === sessionId) {
      setHistoryOpen(false)
      return
    }
    setHistoryLoading(true)
    setSessionError(null)
    try {
      const history = await getAssistantHistory(nextSessionId)
      setSessionId(nextSessionId)
      setMessages(history)
      setPendingAction(null)
      setActivity(null)
      setActivitySteps([])
      setHistoryOpen(false)
    } catch {
      setSessionError('Could not open that conversation.')
    } finally {
      setHistoryLoading(false)
    }
  }, [busy, sessionId])

  const respond = useCallback(async (decision: 'approve' | 'deny') => {
    if (!sessionId || busy) return
    setPendingAction(null)
    setBusy(true)
    const label = decision === 'approve' ? 'Carrying out the approved action' : 'Keeping everything unchanged'
    setActivity({ phase: 'working', message: label })
    setActivitySteps([{ label, state: 'active' }])
    const controller = new AbortController()
    abortRef.current = controller
    try {
      await respondToPendingAction(sessionId, decision, buildHandlers(), controller.signal)
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        textRef.current += '\n\nThe connection dropped. Please try again.'
        setStreamingText(textRef.current)
      }
    } finally {
      finalize()
    }
  }, [sessionId, busy, buildHandlers, finalize])

  const openCommandCenter = () => {
    if (!sessionId || busy) return
    setOpen(false)
    navigate(`/command-center/${sessionId}`)
  }

  // Command Center is the full assistant surface; avoid a duplicate popup there.
  if (location.pathname.startsWith('/command-center')) return null

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open DRPL Assistant"
        title="DRPL Assistant (Ctrl+K)"
        className="fixed bottom-5 right-5 z-50 flex h-14 w-14 items-center justify-center rounded-2xl bg-emerald-600 text-white shadow-[0_12px_32px_rgba(5,150,105,0.3)] transition-all hover:-translate-y-0.5 hover:bg-emerald-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      >
        <Sparkles className="h-5 w-5" aria-hidden />
        {busy && <span className="absolute -right-1 -top-1 h-4 w-4 animate-pulse rounded-full border-2 border-background bg-sky-500" />}
      </button>
    )
  }

  const showWelcome = !historyLoading && messages.length === 0 && !streamingText && !busy

  return (
    <section className="fixed inset-x-3 bottom-3 z-50 flex h-[min(45rem,calc(100vh-1.5rem))] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-2xl sm:inset-x-auto sm:bottom-5 sm:right-5 sm:w-[26rem]" aria-label="DRPL Assistant">
      <header className="border-b border-emerald-500/15 bg-gradient-to-br from-emerald-500/10 via-card to-sky-500/5 px-4 pb-3 pt-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <div className="relative flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-white shadow-sm">
              <Sparkles className="h-4 w-4" aria-hidden />
              <span className={`absolute -right-0.5 -top-0.5 h-3 w-3 rounded-full border-2 border-card ${busy ? 'animate-pulse bg-sky-500' : 'bg-emerald-400'}`} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="truncate text-sm font-bold text-foreground">DRPL Assistant</h2>
                <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold ${busy ? 'bg-sky-500/10 text-sky-700 dark:text-sky-300' : 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'}`}>
                  <span className={`h-1.5 w-1.5 rounded-full ${busy ? 'animate-pulse bg-sky-500' : 'bg-emerald-500'}`} />
                  {busy ? 'Working' : 'Ready'}
                </span>
              </div>
              <p className="mt-0.5 truncate text-xs text-muted-foreground">Your tender work companion</p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <Button variant="outline" size="sm" className="h-8 gap-1.5 border-emerald-500/25 bg-card/80 px-2 text-xs font-bold text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-300" onClick={() => void startNewConversation()} disabled={busy || historyLoading}>
              <Plus className="h-3.5 w-3.5" aria-hidden />New chat
            </Button>
            <Button variant="ghost" size="sm" className="h-8 gap-1.5 px-2 text-xs" onClick={() => setHistoryOpen((value) => !value)} aria-pressed={historyOpen}>
              <History className="h-3.5 w-3.5" aria-hidden />History
            </Button>
            <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => setOpen(false)} aria-label="Close assistant"><X className="h-4 w-4" aria-hidden /></Button>
          </div>
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <span className="inline-flex max-w-[70%] items-center gap-1.5 truncate rounded-full border border-border bg-card/90 px-2.5 py-1 text-[11px] font-semibold text-muted-foreground"><MessageSquareText className="h-3 w-3 shrink-0" aria-hidden />{contextLabel} context</span>
          {historyLoaded && <span className="text-[10px] text-muted-foreground">Saved automatically</span>}
        </div>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
        {sessionError && <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{sessionError}</div>}
        {historyLoading && <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />Loading your conversation…</div>}
        {!sessionError && historyOpen && !historyLoading && <HistoryView conversations={conversations} currentSessionId={sessionId} onSelect={(id) => void selectConversation(id)} onNew={() => void startNewConversation()} onBack={() => setHistoryOpen(false)} />}
        {!sessionError && !historyOpen && !historyLoading && (
          <div className="space-y-4">
            {showWelcome && <WelcomeState disabled={!sessionId} onSelect={(prompt) => void send(prompt)} />}
            {messages.map((message, index) => <MessageBubble key={message.id ?? index} message={message} />)}
            {busy && <ActivityCard activity={activity} steps={activitySteps} hasPartialResult={!!streamingText} />}
            {streamingText && <div className="rounded-2xl border border-border bg-background p-3 text-sm"><MarkdownMessage content={streamingText} variant="compact" /></div>}
            {pendingAction && <ConfirmActionCard action={pendingAction} busy={busy} onApprove={() => void respond('approve')} onDeny={() => void respond('deny')} />}
          </div>
        )}
      </div>

      <footer className="border-t border-border bg-card p-3">
        <div className="mb-2 flex items-center justify-between px-1 text-[11px]">
          <span className="text-muted-foreground">{busy ? 'You can close this — I’ll keep working.' : 'This chat is saved automatically.'}</span>
          <button type="button" onClick={openCommandCenter} disabled={!sessionId || busy} title={busy ? 'Available when the current work finishes' : undefined} className="inline-flex items-center gap-1 font-bold text-emerald-700 hover:text-emerald-800 disabled:opacity-40 dark:text-emerald-400">Open Command Center <ExternalLink className="h-3 w-3" aria-hidden /></button>
        </div>
        <div className="flex items-end gap-2 rounded-xl border border-input bg-background p-2 focus-within:ring-2 focus-within:ring-ring">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }}
            rows={1}
            placeholder="Ask anything or add instructions…"
            aria-label="Message the assistant"
            disabled={!!sessionError}
            className="max-h-28 min-h-9 flex-1 resize-none bg-transparent px-2 py-2 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-50"
          />
          <Button size="icon" className="h-9 w-9 rounded-lg" onClick={() => void send()} disabled={busy || !input.trim() || !sessionId} aria-label="Send message">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}</Button>
        </div>
      </footer>
    </section>
  )
}

function WelcomeState({ disabled, onSelect }: { disabled: boolean; onSelect: (prompt: string) => void }) {
  return (
    <div>
      <div className="mb-4"><p className="text-base font-bold text-foreground">Hi — what would you like to move forward?</p><p className="mt-1 text-sm leading-5 text-muted-foreground">I can work across DRPL, explain what you’re viewing, and ask before making important changes.</p></div>
      <button type="button" disabled={disabled} onClick={() => onSelect(QUICK_TASKS[0].prompt)} className="w-full rounded-2xl border border-emerald-500/20 bg-emerald-500/5 p-4 text-left transition-colors hover:bg-emerald-500/10 disabled:opacity-50">
        <div className="flex items-start gap-3"><span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"><ScanSearch className="h-4 w-4" /></span><span className="min-w-0"><span className="block text-sm font-bold text-foreground">Check current tender opportunities</span><span className="mt-1 block text-xs leading-5 text-muted-foreground">Compare live tenders with your company profile and prepare a ranked shortlist.</span></span><ArrowRight className="mt-1 h-4 w-4 shrink-0 text-emerald-700 dark:text-emerald-400" /></div>
      </button>
      <p className="mb-2 mt-5 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">Popular tasks</p>
      <div className="grid grid-cols-2 gap-2">
        {QUICK_TASKS.slice(1).map(({ label, prompt, icon: Icon }) => <button key={label} type="button" disabled={disabled} onClick={() => onSelect(prompt)} className="flex min-h-20 flex-col items-start gap-2 rounded-xl border border-border bg-background p-3 text-left text-xs font-semibold text-foreground transition-colors hover:bg-muted disabled:opacity-50"><Icon className="h-4 w-4 text-emerald-700 dark:text-emerald-400" />{label}</button>)}
      </div>
    </div>
  )
}

function ActivityCard({ activity, steps, hasPartialResult }: { activity: AssistantStatus | null; steps: ActivityStep[]; hasPartialResult: boolean }) {
  const progress = activity?.total ? Math.min(100, Math.round(((activity.completed || 0) / activity.total) * 100)) : null
  return (
    <div className="rounded-2xl border border-emerald-500/20 bg-emerald-500/5 p-4" aria-live="polite">
      <div className="flex items-start gap-3"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"><Loader2 className="h-4 w-4 animate-spin" /></span><div className="min-w-0"><p className="text-sm font-bold text-foreground">{activity?.message || 'Working on your request'}</p>{activity?.detail && <p className="mt-1 text-xs leading-5 text-muted-foreground">{activity.detail}</p>}</div></div>
      {steps.length > 0 && <div className="mt-4 space-y-2.5 border-l border-emerald-500/20 pl-4">{steps.map((step, index) => <div key={`${step.label}-${index}`} className="flex items-start gap-2 text-xs">{step.state === 'completed' ? <span className="-ml-[1.6rem] flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-white"><Check className="h-3 w-3" /></span> : <span className="-ml-[1.6rem] flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 border-emerald-600 bg-card"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-600" /></span>}<span className={step.state === 'active' ? 'font-semibold text-foreground' : 'text-muted-foreground'}>{step.label}</span>{step.total ? <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">{step.completed || 0}/{step.total}</span> : null}</div>)}</div>}
      {progress !== null && <div className="mt-4"><div className="h-1.5 overflow-hidden rounded-full bg-emerald-500/10"><div className="h-full rounded-full bg-emerald-600 transition-all" style={{ width: `${progress}%` }} /></div><p className="mt-1.5 text-[10px] text-muted-foreground">{progress}% complete</p></div>}
      {hasPartialResult && <div className="mt-3 rounded-xl border border-border bg-card px-3 py-2 text-xs text-muted-foreground">Useful results will appear below as soon as they are ready.</div>}
    </div>
  )
}

function HistoryView({ conversations, currentSessionId, onSelect, onNew, onBack }: { conversations: AssistantSession[]; currentSessionId: number | null; onSelect: (id: number) => void; onNew: () => void; onBack: () => void }) {
  return (
    <div>
      <div className="mb-4 flex items-start justify-between gap-3"><div><h3 className="text-sm font-bold text-foreground">Conversations</h3><p className="mt-0.5 text-xs text-muted-foreground">Start fresh or return to an earlier chat.</p></div><Button variant="ghost" size="sm" onClick={onBack}>Back</Button></div>
      <Button className="mb-3 w-full justify-start gap-2 rounded-xl" onClick={onNew}><Plus className="h-4 w-4" />Start a new chat</Button>
      {conversations.length === 0 ? <div className="rounded-xl border border-dashed border-border p-6 text-center text-sm text-muted-foreground"><Clock3 className="mx-auto mb-2 h-5 w-5" />No conversations yet.</div> : <div className="space-y-2">{conversations.map((conversation) => <button key={conversation.session_id} type="button" onClick={() => onSelect(conversation.session_id)} className={`w-full rounded-xl border p-3 text-left transition-colors hover:bg-muted ${conversation.session_id === currentSessionId ? 'border-emerald-500/35 bg-emerald-500/5' : 'border-border bg-background'}`}><div className="flex items-center justify-between gap-2"><span className="truncate text-xs font-bold text-foreground">{conversation.title || 'New conversation'}</span>{conversation.session_id === currentSessionId && <span className="shrink-0 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide text-emerald-700 dark:text-emerald-300">Current</span>}</div>{conversation.preview && <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{conversation.preview}</p>}<div className="mt-2 flex items-center gap-2 text-[10px] text-muted-foreground"><span>{conversation.message_count || 0} messages</span>{conversation.created_at && <><span aria-hidden>·</span><time>{new Date(conversation.created_at).toLocaleDateString([], { dateStyle: 'medium' })}</time></>}</div></button>)}</div>}
    </div>
  )
}

function MessageBubble({ message }: { message: AssistantMessage }) {
  const [showDetail, setShowDetail] = useState(false)
  if (message.role === 'user') return <div className="ml-8 rounded-2xl rounded-br-md bg-muted px-3 py-2.5 text-sm text-foreground">{message.content}</div>
  return (
    <div className="rounded-2xl rounded-bl-md border border-border bg-background px-3 py-2.5 text-sm"><MarkdownMessage content={message.content} variant="compact" />{message.detail && <><button type="button" onClick={() => setShowDetail((value) => !value)} aria-expanded={showDetail} className="mt-2 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"><ChevronDown className={`h-3 w-3 transition-transform ${showDetail ? 'rotate-180' : ''}`} />{showDetail ? 'Hide work log' : 'Show work log'}</button>{showDetail && <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-2 text-xs text-muted-foreground">{message.detail}</pre>}</>}</div>
  )
}
