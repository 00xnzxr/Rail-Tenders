import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Send, Square, Bot, Loader2, ArrowDown,
  Search, ClipboardList, FileText, DollarSign, Plus, MessageSquare,
  Paperclip, X, Layers, LayoutGrid, Sparkles,
  HelpCircle, BookOpen,
} from 'lucide-react';
import { MarkdownMessage, CollapsibleMarkdown } from '../components/command-center/chat/MarkdownMessage';
import { RunCostLine, type RunCost } from '../components/command-center/chat/RunCostLine';

import {
  API_BASE, createCommandCenterSession, getCommandCenterSession,
  getCommandCenterHistory, getSessionArtifacts, updateArtifact,
  getSessionSuggestions, listCommandCenterSessions, uploadChatAttachments,
  initSessionWorkspace, deleteCommandCenterSession,
  listClarifications, cancelClarification, answerClarificationStream,
  streamCommandCenter, openRunStream, cancelRun,
  saveActiveRun, loadActiveRun, clearActiveRun, getActiveRun,
  respondToDecisionPlan,
} from '../lib/api';
import type { PendingClarification } from '../lib/api';
import type { ChatMessage, ChatAttachment, Artifact, Suggestion, RoutingInfo, ArtifactCreatedEvent, CommandCenterSession, TimelineEvent, TimelineBudget, PendingPlan, AgentNotice } from '../types/command-center';
import ArtifactPanel from '../components/command-center/ArtifactPanel';
import DecisionMakerTimeline from '../components/command-center/DecisionMakerTimeline';
import ArtifactCard from '../components/command-center/ArtifactCard';
import { AgentNoticeList } from '../components/command-center/AgentErrorBubble';
import PipelineStatusCard from '../components/agents/PipelineStatusCard';
import SuggestionBar from '../components/command-center/SuggestionBar';
import WorkspaceTabContent from '../components/command-center/WorkspaceTabContent';
import DocumentsTabContent from '../components/command-center/DocumentsTabContent';
import ClarificationModal from '../components/command-center/ClarificationModal';
import DecisionPlanCard from '../components/command-center/DecisionPlanCard';
import PlanExecutionProgress from '../components/command-center/PlanExecutionProgress';
import type { PlanProgressStep } from '../components/command-center/PlanExecutionProgress';
import { AgentStatusPill } from '../components/command-center/AgentStatusPill';
import SessionSidebar from '../components/command-center/SessionSidebar';
import SessionSwitcherPalette from '../components/command-center/SessionSwitcherPalette';
import SessionFilterBar from '../components/command-center/SessionFilterBar';
import { useSessionFilters } from '../hooks/useSessionFilters';

/** Strip internal file-content markers that should never appear in chat UI. */
function stripFileMarkers(text: string): string {
  return text
    // [FILE CONTENT: filename] ... (up to next marker or end)
    .replace(/\[FILE CONTENT:\s*[^\]]*\][\s\S]*?(?=\[FILE CONTENT:|\[ATTACHED FILES\]|$)/g, '')
    // [ATTACHED FILES] header + listing lines
    .replace(/\[ATTACHED FILES\]\s*\n(?:\s*-\s+[^\n]+\n?)*/g, '')
    .replace(/\[ATTACHED FILES\]\s*/g, '')
    // Truncation notices
    .replace(/\[\.\.\.\s*Document truncated[^\]]*\]/g, '')
    .replace(/\[Content truncated\s*[\u2014\u2014-]+\s*file limit reached\]/g, '')
    .trim();
}

function cleanSessionTitle(session: any): string {
  const raw = String(session?.tender_title || session?.title || (session?.tender_id ? `Tender #${session.tender_id}` : 'New chat')).trim();
  return raw.replace(/^command\s*center\s*/i, '').replace(/\s*\(\d+\)\s*$/, '').trim() || 'New chat';
}

function messageTime(value?: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/** Defence-in-depth: collapse duplicate assistant rows that slip through the
 *  backend (e.g. if a wrapper and the router both save the same turn).
 *  Keeps the row with richer metadata (routed_from set) when duplicates exist.
 */
function dedupMessages(msgs: ChatMessage[]): ChatMessage[] {
  const seen = new Map<string, number>(); // key -> index in out
  const out: ChatMessage[] = [];
  for (const m of msgs) {
    if (m.role !== 'assistant' || !m.content) {
      out.push(m);
      continue;
    }
    const sig = m.content.trim().slice(0, 160);
    const key = `${m.role}|${sig}`;
    const existingIdx = seen.get(key);
    if (existingIdx === undefined) {
      seen.set(key, out.length);
      out.push(m);
      continue;
    }
    const existing = out[existingIdx];
    const incomingRicher = !!m.routed_from && !existing.routed_from;
    if (incomingRicher) out[existingIdx] = m;
  }
  return out;
}

const SUGGESTION_PROMPTS = [
  { icon: Search, text: 'Analyze tender documents and flag rejection risks', color: 'text-purple-500 bg-purple-50 dark:bg-purple-500/15' },
  { icon: ClipboardList, text: 'Generate submission checklist from tender requirements', color: 'text-emerald-500 bg-emerald-50 dark:bg-emerald-500/15' },
  { icon: BookOpen, text: 'Find and extract all annexures from tender documents', color: 'text-indigo-500 bg-indigo-50 dark:bg-indigo-500/15' },
  { icon: FileText, text: 'Draft a technical proposal for this tender', color: 'text-accent bg-accent/10' },
  { icon: DollarSign, text: 'Estimate project costs with detailed breakdown', color: 'text-amber-500 bg-amber-50 dark:bg-amber-500/15' },
  { icon: Layers, text: 'Prepare the required tender documents', color: 'text-teal-500 bg-teal-50 dark:bg-teal-500/15' },
];

type ActiveTab = 'chat' | 'workspace' | 'documents';

export default function CommandCenterPage() {
  const { sessionId: routeSessionId, id: routeTenderId } = useParams();
  const navigate = useNavigate();

  // Session state
  const [session, setSession] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  // Tab state
  const [activeTab, setActiveTab] = useState<ActiveTab>('chat');
  const [newDocsAlert, setNewDocsAlert] = useState(false);
  const [docsCount, setDocsCount] = useState(0);

  // Artifact full-screen view
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  // When true, the artifact panel expands to cover the whole chat area.
  const [artifactFullScreen, setArtifactFullScreen] = useState(false);
  const userClosedPanel = useRef(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const sessionFilters = useSessionFilters();
  const isSendingRef = useRef(false); // mutex — prevents double-send on rapid Enter / click
  // The run id this page is currently streaming, so Stop can reach the worker
  // rather than only the browser's own connection. Declared with the other
  // refs: `sendMessage` reads it, and it was defined nine hundred lines below.
  const activeRunIdRef = useRef<string | null>(null);
  const [stopping, setStopping] = useState(false);

  // Chat state
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [routingInfo, setRoutingInfo] = useState<RoutingInfo | null>(null);
  // Decision Maker streaming state — live Thought/Action/Observation timeline
  const [streamingTimeline, setStreamingTimeline] = useState<TimelineEvent[]>([]);
  const [streamingBudget, setStreamingBudget] = useState<TimelineBudget | undefined>(undefined);
  // Reliability indicator — populated when the backend auto-continues a
  // truncated response, retries an empty one, or recovers from thinking
  // burning the whole budget. Cleared on agent_complete or final 'done'.
  // Without this, those recovery paths happen invisibly and users can't
  // tell why a response is taking longer than usual.
  const [reliabilityStatus, setReliabilityStatus] = useState<{
    kind: 'continuing' | 'resumed' | 'retried_empty' | 'truncated' | 'empty' | 'retried_no_thinking'
        | 'prerequisite_running' | 'prerequisite_complete' | 'prerequisite_failed'
        | 'analyzer_progress' | 'costing_batch_progress' | 'costing_boq_extract';
    agent?: string;
    attempt?: number;
    maxAttempts?: number;
    detail?: string;
    // analyzer_progress fields — render "Reading 2 of 5 documents…"
    phase?: 'starting' | 'per_doc_complete' | 'synthesis_start' | 'complete';
    completed?: number;
    total?: number;
    // costing_batch_progress fields — render "Costing batch 2 of 6…"
    batch?: number;
    totalBatches?: number;
    linesDone?: number;
    linesTotal?: number;
  } | null>(null);
  // Live plan-execution progress (decision_maker post-approval path).
  const [streamingPlanProgress, setStreamingPlanProgress] = useState<{
    title?: string | null;
    steps: PlanProgressStep[];
  } | null>(null);
  // One-line "what the agent is doing right now" pill, fed by agent_status
  // SSE events (LLM thinking + tool start/end). Cleared when the underlying
  // step finishes, when tokens start streaming, or when the run ends.
  const [agentStatus, setAgentStatus] = useState<{
    runId: string;
    message: string;
    startedAtMs: number;
  } | null>(null);

  const [error, setError] = useState('');
  const [abortController, setAbortController] = useState<AbortController | null>(null);

  // Artifact state
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedArtifactId, setSelectedArtifactId] = useState<number | null>(null);


  // Session list (shown when no sessionId in route)
  const [sessionList, setSessionList] = useState<CommandCenterSession[]>([]);
  const filteredSessionList = useMemo(
    () => sessionFilters.apply(sessionList),
    [sessionFilters.apply, sessionList],
  );
  // Initial-only loading flag — flips false after the first listCommandCenterSessions resolves.
  const [sessionListLoading, setSessionListLoading] = useState(true);

  // Bulk select + delete
  const [deleteTargetId, setDeleteTargetId] = useState<number | null>(null);

  // Suggestions
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);

  // Pipeline
  const [pipelineState, setPipelineState] = useState<Record<string, boolean>>({});

  // Workspace init
  const [workspaceInitLoading, setWorkspaceInitLoading] = useState(false);

  // Clarifications (agent-initiated popups)
  const [clarifications, setClarifications] = useState<PendingClarification[]>([]);
  const [activeClarificationId, setActiveClarificationId] = useState<number | null>(null);
  const [clarificationSubmitting, setClarificationSubmitting] = useState(false);

  // File attachments
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Scroll
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // --- Initialization ---
  useEffect(() => {
    initSession();
  }, [routeSessionId, routeTenderId]);


  const initSession = async () => {
    // Already showing this session — do not reload it.
    //
    // The composer creates a session and syncs the URL mid-send
    // (/command-center -> /command-center/300). That fires this effect, and the
    // reset below wiped the optimistic message and the streaming state of a run
    // that had only just started: the screen went back to the empty task-picker
    // while the run continued server-side. Switching sessions from the sidebar
    // still reloads, because the id genuinely differs there.
    if (routeSessionId && session && session.id === Number(routeSessionId)) {
      setLoading(false);
      return;
    }

    setSession(null);
    setMessages([]);
    setArtifacts([]);
    setSuggestions([]);
    setPipelineState({});
    setActiveTab('chat');
    setArtifactPanelOpen(false);
    setArtifactFullScreen(false);
    userClosedPanel.current = false;
    setError('');
    setLoading(true);
    try {
      if (routeSessionId) {
        // Also populate the sidebar list in parallel — non-blocking, non-fatal.
        listCommandCenterSessions()
          .then((list) => {
            setSessionList(list);
            setSessionListLoading(false);
          })
          .catch(() => {
            setSessionListLoading(false);
          });

        const sess = await getCommandCenterSession(Number(routeSessionId));
        setSession(sess);
        setPipelineState(sess.pipeline_state || {});

        const [history, arts] = await Promise.all([
          getCommandCenterHistory(sess.id),
          getSessionArtifacts(sess.id),
        ]);
        const mapped: ChatMessage[] = history.map((h: any) => {
          const msgArtifacts = arts.filter((a: any) => a.message_id === h.id);
          const md = h.metadata_json || h.metadata || null;
          // Rebuild the DecisionPlanCard from metadata so a tab refresh keeps
          // the pretty plan UI (with the right approved / denied / pending state).
          const storedPlan = md?.pending_plan;
          const planStatus = md?.plan_status;
          const planDecision = ['approve', 'change', 'deny'].includes(planStatus)
            ? (planStatus as 'approve' | 'change' | 'deny')
            : null;
          return {
            id: h.id,
            role: h.role,
            content: h.content,
            tool_calls: h.tool_calls,
            output_type: h.output_type,
            routed_from: h.routed_from,
            created_at: h.created_at,
            attachments: h.attachments,
            metadata: md,
            // Rehydrate decision-maker timeline from saved ProposalMessage metadata
            timeline: md && Array.isArray(md.decision_trace) ? md.decision_trace : undefined,
            pendingPlan: storedPlan && typeof storedPlan === 'object'
              ? {
                  title: storedPlan.title || 'Proposed plan',
                  reasoning: storedPlan.reasoning || '',
                  steps: Array.isArray(storedPlan.steps) ? storedPlan.steps : [],
                }
              : undefined,
            planDecision,
            artifact_refs: msgArtifacts.length > 0
              ? msgArtifacts.map((a: any) => ({
                  artifact_id: a.id,
                  artifact_type: a.artifact_type,
                  title: a.title,
                  version: a.version,
                }))
              : undefined,
          };
        });
        setMessages(dedupMessages(mapped));
        setArtifacts(arts);

        const sug = await getSessionSuggestions(sess.id);
        setSuggestions(sug);

        // Re-surface any clarification the user didn't answer last time.
        try {
          const pending = await listClarifications(sess.id, 'pending');
          if (pending.length > 0) {
            setClarifications(pending);
            setActiveClarificationId(pending[0].id);
          }
        } catch {
          /* non-fatal */
        }

        // Browser-close survival: if a run was in flight when the user left,
        // reattach to its event stream and keep streaming tokens into the UI.
        //
        // The local pointer is only a shortcut. It lives in the browser that
        // started the run, so it is missing on a second device, in another
        // browser, and after cleared site data — all cases where the run is
        // very much alive and used to be unreachable, which looks exactly like
        // a run that died. Ask the backend when we have no pointer of our own.
        const activeRun = loadActiveRun(sess.id);
        if (activeRun) {
          resumeActiveRun(sess.id, activeRun.run_id);
        } else {
          const serverRunId = await getActiveRun(sess.id);
          if (serverRunId) {
            saveActiveRun(sess.id, serverRunId);
            resumeActiveRun(sess.id, serverRunId);
          }
        }
      } else if (routeTenderId) {
        // Populate sidebar list in parallel — non-blocking, non-fatal.
        listCommandCenterSessions()
          .then((list) => {
            setSessionList(list);
            setSessionListLoading(false);
          })
          .catch(() => {
            setSessionListLoading(false);
          });

        const sess = await createCommandCenterSession({ tender_id: Number(routeTenderId) });
        setSession(sess);
        setPipelineState(sess.pipeline_state || {});
        navigate(`/command-center/${sess.id}`, { replace: true });

        const history = await getCommandCenterHistory(sess.id);
        setMessages(dedupMessages(history.map((h: any) => ({
          id: h.id, role: h.role, content: h.content,
          output_type: h.output_type, routed_from: h.routed_from, created_at: h.created_at,
          attachments: h.attachments,
        }))));

        const sug = await getSessionSuggestions(sess.id);
        setSuggestions(sug);
      } else {
        // Bare /command-center — land directly in the chat shell (ChatGPT/
        // Claude-style) with the conversation sidebar populated and an empty
        // composer. `session` stays null; the first message lazily creates a
        // standalone session (see sendMessage).
        const sessions = await listCommandCenterSessions();
        setSessionList(sessions);
        setSessionListLoading(false);
      }
    } catch (err) {
      setError('Failed to initialize session');
    } finally {
      setLoading(false);
    }
  };

  // Global Cmd/Ctrl+K to toggle the session switcher palette. Captures
  // before chat/textarea focus so it works while typing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  // --- Auto-scroll ---
  useEffect(() => {
    if (!showScrollBtn && activeTab === 'chat') {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, streamingText]);

  // Force-scroll the chat to the bottom whenever the plan executor pauses
  // and surfaces a "Run next step" button, so the button is always visible

  const handleScroll = useCallback(() => {
    if (!chatContainerRef.current) return;
    const { scrollHeight, scrollTop, clientHeight } = chatContainerRef.current;
    setShowScrollBtn(scrollHeight - scrollTop - clientHeight > 100);
  }, []);

  // --- Tab switching ---
  const switchTab = (tab: ActiveTab) => {
    setActiveTab(tab);
    if (tab === 'documents') setNewDocsAlert(false);
  };

  // --- Send Message ---
  // Handle reliability events emitted by the backend's auto-recovery layer
  // (continuation on stop_reason=max_tokens, retry on empty content, retry
  // without thinking when thinking burned the budget). These are surfaced
  // through SSE so users understand why the response is taking a moment to
  // finish — without this they see a frozen UI and assume the agent hung.
  const handleReliabilityEvent = (eventType: string, data: any) => {
    switch (eventType) {
      case 'assistant_continuing':
        setReliabilityStatus({
          kind: 'continuing',
          agent: data?.agent,
          attempt: data?.attempt,
          maxAttempts: data?.max_attempts,
          detail: data?.reason,
        });
        break;
      case 'assistant_resumed':
        setReliabilityStatus({
          kind: 'resumed',
          agent: data?.agent,
          detail: data?.continuations
            ? `after ${data.continuations} continuation${data.continuations === 1 ? '' : 's'}`
            : undefined,
        });
        // Auto-fade the success indicator after a moment.
        setTimeout(() => {
          setReliabilityStatus((prev) => (prev?.kind === 'resumed' ? null : prev));
        }, 1500);
        break;
      case 'assistant_retried_empty':
        setReliabilityStatus({
          kind: 'retried_empty',
          agent: data?.agent,
          detail: data?.stop_reason,
        });
        break;
      case 'assistant_truncated':
        setReliabilityStatus({
          kind: 'truncated',
          agent: data?.agent,
        });
        break;
      case 'assistant_empty_content':
        setReliabilityStatus({
          kind: 'empty',
          agent: data?.agent,
          detail: data?.stop_reason,
        });
        break;
      case 'assistant_retried_no_thinking':
        setReliabilityStatus({
          kind: 'retried_no_thinking',
          agent: data?.agent,
        });
        break;
      case 'prerequisite_running':
        // Auto-chained sub-agent (e.g. analyzer-before-costing). Surface a
        // distinct indicator so users see WHY the response is taking longer
        // than usual on a fresh tender — the analyzer is running first.
        setReliabilityStatus({
          kind: 'prerequisite_running',
          agent: data?.agent_key,
          detail: data?.display_name || data?.agent_key,
        });
        break;
      case 'prerequisite_complete':
        setReliabilityStatus({
          kind: 'prerequisite_complete',
          agent: data?.agent_key,
          detail: data?.summary_chars
            ? `${data.summary_chars} chars of analysis ready`
            : undefined,
        });
        // Brief confirmation flash, then clear so the costing-streaming
        // indicator (or actual response) takes over.
        setTimeout(() => {
          setReliabilityStatus((prev) =>
            prev?.kind === 'prerequisite_complete' ? null : prev
          );
        }, 1200);
        break;
      case 'prerequisite_failed':
        setReliabilityStatus({
          kind: 'prerequisite_failed',
          agent: data?.agent_key,
          detail: data?.reason,
        });
        // Hold the failure pill visible for 30s. The parent agent runs
        // anyway with a degraded path, but the user needs time to read
        // WHY the prerequisite failed — a 3-second auto-dismiss was too
        // brief and made the error feel mysterious. If the real response
        // starts streaming and pushes useful content, the user can
        // visually compare. Clears on agent_complete / done / new turn.
        setTimeout(() => {
          setReliabilityStatus((prev) =>
            prev?.kind === 'prerequisite_failed' ? null : prev
          );
        }, 30000);
        break;
      case 'analyzer_progress':
        // Live progress signal from the v2 deep analyzer. Rendered as a
        // status pill so the user sees activity during the 2–5 minute
        // multi-PDF analysis instead of staring at "Thinking..." and
        // wondering whether the worker has hung.
        setReliabilityStatus({
          kind: 'analyzer_progress',
          phase: data?.phase,
          completed: data?.completed,
          total: data?.total,
          detail: data?.doc_name,
        });
        // 'complete' phase auto-dismisses since the actual response chunks
        // will start landing immediately after.
        if (data?.phase === 'complete') {
          setTimeout(() => {
            setReliabilityStatus((prev) =>
              prev?.kind === 'analyzer_progress' ? null : prev
            );
          }, 800);
        }
        break;
      case 'costing_batch_progress':
        // Live progress from the batched costing path (large tenders). Shows
        // "Costing batch 2 of 6 — 90/300 line items…" so the user sees activity
        // during a multi-minute, multi-pass costing run.
        setReliabilityStatus({
          kind: 'costing_batch_progress',
          batch: data?.batch,
          totalBatches: data?.total_batches,
          linesDone: data?.lines_done,
          linesTotal: data?.lines_total,
        });
        break;
      case 'costing_boq_extract':
        // The costing flow is auto-extracting the NIT bidding schedule
        // (lightweight BOQ parse, not the full tender analysis) before costing.
        if (data?.phase === 'complete') {
          setTimeout(() => {
            setReliabilityStatus((prev) =>
              prev?.kind === 'costing_boq_extract' ? null : prev
            );
          }, 600);
        } else {
          setReliabilityStatus({ kind: 'costing_boq_extract' });
        }
        break;
      case 'tender_enriched':
        // Backend renamed the tender + session after extracting the real
        // reference / name of work. Update the local session title so the
        // sidebar reflects the new name without a page reload.
        if (data?.title) {
          setSession((prev: any) => prev ? { ...prev, title: data.title } : prev);
        }
        break;
    }
  };

  const sendMessage = async (text?: string) => {
    const userMsg = (text || input).trim();
    if ((!userMsg && pendingFiles.length === 0) || streaming) return;
    if (streamingPlanProgress) return;
    if (isSendingRef.current) return;
    isSendingRef.current = true;
    const finalMsg = userMsg || `Please analyze the attached file${pendingFiles.length > 1 ? 's' : ''}`;
    const displayMsg = userMsg || `[Attached ${pendingFiles.length} file${pendingFiles.length > 1 ? 's' : ''}]`;

    setInput('');
    if (inputRef.current) inputRef.current.style.height = 'auto';
    setError('');
    setRoutingInfo(null);

    // Ensure we're on chat tab when sending
    if (activeTab !== 'chat') setActiveTab('chat');

    // Lazy session creation (ChatGPT-style): the fresh "new chat" view has no
    // session yet — create a standalone one on the first message, then sync the
    // URL so the conversation is deep-linkable. `activeSession` is used for the
    // rest of this send so we don't depend on the async setSession flush.
    let activeSession = session;
    if (!activeSession) {
      try {
        activeSession = await createCommandCenterSession({ title: 'New Session' });
        setSession(activeSession);
        setSessionList((prev) => [activeSession as CommandCenterSession, ...prev]);
        // replace so Back doesn't return to the empty composer; keeps history clean.
        navigate(`/command-center/${activeSession.id}`, { replace: true });
      } catch {
        setError('Could not start a new chat. Please try again.');
        isSendingRef.current = false;
        return;
      }
    }

    let fileIds: number[] = [];
    const filesToAttach = [...pendingFiles];
    if (filesToAttach.length > 0) {
      setUploading(true);
      try {
        const uploaded = await uploadChatAttachments(activeSession.id, filesToAttach);
        fileIds = uploaded.map((u: any) => u.id);
        setPendingFiles([]);
      } catch {
        setError('Failed to upload files');
        setUploading(false);
        return;
      }
      setUploading(false);
    }

    const userMessage: ChatMessage = {
      id: Date.now(),
      role: 'user',
      content: displayMsg,
      created_at: new Date().toISOString(),
      attachments: filesToAttach.map((f) => ({
        id: 0,
        file_name: f.name,
        file_type: f.type,
        file_size: f.size,
      })),
    };
    setMessages((prev) => [...prev, userMessage]);

    userClosedPanel.current = false;
    let currentArtifactRefs: ArtifactCreatedEvent[] = [];
    // Declared at function scope so the AbortError catch block can still
    // snapshot them into the "stopped" assistant message.
    let currentTimeline: TimelineEvent[] = [];
    let currentBudget: TimelineBudget | undefined = undefined;
    let currentPendingPlan: PendingPlan | null = null;
    let currentPlanProgress: { title?: string | null; steps: PlanProgressStep[] } | null = null;
    // Typed agent_error / agent_warning events accumulator. Rendered as
    // distinct bubbles below the assistant message — NOT concatenated into
    // the streaming text (which was the line-884 bug).
    let currentAgentNotices: AgentNotice[] = [];

    const controller = new AbortController();
    setAbortController(controller);
    setStreaming(true);
    setStreamingText('');
    setStreamingTimeline([]);
    setStreamingBudget(undefined);

    try {
      const { response, runId } = await streamCommandCenter({
        message: finalMsg,
        file_ids: fileIds,
        proposal_session_id: activeSession.id,
        tender_id: (activeSession as any).tender_id || undefined,
      }, controller.signal);

      if (!response.ok) {
        const errBody = await response.text().catch(() => '');
        throw new Error(`HTTP ${response.status}: ${errBody}`);
      }

      // Persist the run id so a reload during generation can resume, and keep
      // it where Stop can find it.
      activeRunIdRef.current = runId;
      if (runId) saveActiveRun(activeSession.id, runId);

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let fullText = '';
      let currentOutputType = 'general';
      let currentRoutedFrom = '';
      // What the run cost, from the `done` event. Also saved on the message
      // server-side, which is what survives a reload.
      let currentRunCost: RunCost | null = null;
      let buffer = '';

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          let eventType = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              const dataStr = line.slice(6);
              try {
                const data = JSON.parse(dataStr);

                switch (eventType) {
                  case 'session':
                    break;
                  case 'routing':
                    setRoutingInfo({
                      intent: data.intent,
                      agents: data.agents || [],
                      agentNames: data.agent_names || [],
                      reasoning: data.reasoning || undefined,
                    });
                    break;
                  case 'agent_start':
                    setRoutingInfo((prev) => ({
                      ...prev!,
                      intent: prev?.intent || '',
                      agents: prev?.agents || [data.agent_key],
                      agentNames: [data.display_name || data.agent_key],
                    }));
                    break;
                  case 'assistant_continuing':
                  case 'assistant_resumed':
                  case 'assistant_retried_empty':
                  case 'assistant_truncated':
                  case 'assistant_empty_content':
                  case 'assistant_retried_no_thinking':
                  case 'prerequisite_running':
                  case 'prerequisite_complete':
                  case 'prerequisite_failed':
                  case 'analyzer_progress':
                  case 'costing_batch_progress':
                  case 'costing_boq_extract':
                  case 'tender_enriched':
                    handleReliabilityEvent(eventType, data);
                    break;
                  case 'token':
                    if (data.content) {
                      fullText += data.content;
                      setStreamingText(fullText);
                      // First real token after a continuation/retry means
                      // we've successfully resumed — clear any lingering
                      // indicator so the UI doesn't keep showing it.
                      setReliabilityStatus((prev) =>
                        prev && prev.kind !== 'resumed' ? null : prev
                      );
                      // Tokens are arriving — the agent is no longer
                      // "thinking" or "running a tool"; clear the status pill.
                      setAgentStatus(null);
                    }
                    break;
                  case 'token_reset':
                    // A tool-using agent narrates on its way to a tool call
                    // ("Let me search for that"). That text is not the answer,
                    // and this accumulator is what gets saved as the message.
                    fullText = '';
                    setStreamingText('');
                    break;
                  case 'agent_status': {
                    const phase = data.phase as string;
                    const runId = String(data.run_id ?? '');
                    if (phase === 'tool_done') {
                      setAgentStatus((prev) =>
                        prev && prev.runId === runId ? null : prev
                      );
                    } else if (phase === 'tool_running' || phase === 'llm_thinking') {
                      const startedAtMs =
                        typeof data.started_at_ms === 'number'
                          ? data.started_at_ms
                          : Date.now();
                      setAgentStatus({
                        runId,
                        message: data.message || 'Working',
                        startedAtMs,
                      });
                    }
                    break;
                  }
                  case 'agent_complete':
                    currentOutputType = data.output_type || 'general';
                    currentRoutedFrom = data.agent_key || '';
                    if (Array.isArray(data.trace)) {
                      currentTimeline = data.trace as TimelineEvent[];
                      setStreamingTimeline(currentTimeline);
                    }
                    if (data.pending_plan) {
                      currentPendingPlan = data.pending_plan as PendingPlan;
                    }
                    break;
                  case 'plan_proposed':
                    currentPendingPlan = {
                      title: data.title || 'Proposed plan',
                      reasoning: data.reasoning || '',
                      steps: Array.isArray(data.steps) ? data.steps : [],
                    };
                    break;
                  case 'plan_execution_started':
                    currentPlanProgress = {
                      title: data.title || null,
                      steps: [],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  case 'plan_step_progress': {
                    const next: PlanProgressStep = {
                      index: data.index ?? 0,
                      total: data.total ?? 0,
                      agent: data.agent ?? null,
                      agent_display_name: data.agent_display_name ?? null,
                      description: data.description ?? '',
                      status: (data.status as PlanProgressStep['status']) || 'running',
                      error: data.error ?? null,
                    };
                    if (!currentPlanProgress) {
                      currentPlanProgress = { title: null, steps: [] };
                    }
                    const existingIdx = currentPlanProgress.steps.findIndex((s) => s.index === next.index);
                    if (existingIdx >= 0) {
                      currentPlanProgress.steps[existingIdx] = next;
                    } else {
                      currentPlanProgress.steps = [...currentPlanProgress.steps, next];
                    }
                    currentPlanProgress = {
                      ...currentPlanProgress,
                      steps: [...currentPlanProgress.steps],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  }
                  case 'plan_execution_complete':
                    break;
                  case 'workspace_initialized':
                    // Non-fatal notification — frontend can refresh its
                    // workspace view later. No UI change required here.
                    break;
                  case 'decision_thought': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'thought', content: data.content || '',
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_action': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'action',
                      tool: data.tool, input_preview: data.input_preview,
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_observation': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'observation',
                      result_preview: data.result_preview, is_error: !!data.is_error,
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_budget':
                    currentBudget = {
                      iteration: data.iteration ?? 0,
                      max_iterations: data.max_iterations ?? 15,
                      elapsed_s: data.elapsed_s ?? 0,
                      max_execution_time: data.max_execution_time,
                    };
                    setStreamingBudget(currentBudget);
                    break;
                  case 'decision_final':
                    // Final answer content mirrored by token events; no-op
                    break;
                  case 'artifact_created': {
                    const evt = data as ArtifactCreatedEvent;
                    currentArtifactRefs = [...currentArtifactRefs, evt];
                    getSessionArtifacts(activeSession.id).then((arts) => {
                      setArtifacts(arts);
                      setSelectedArtifactId(evt.artifact_id);
                      if (!userClosedPanel.current) setArtifactPanelOpen(true);
                    });
                    break;
                  }
                  case 'clarification_request': {
                    const row: PendingClarification = {
                      id: data.clarification_id,
                      agent_key: data.agent_key || 'assistant',
                      question: data.question || '',
                      options: data.options || [],
                      context: data.context || {},
                      status: 'pending',
                      answer: null,
                      created_at: new Date().toISOString(),
                      answered_at: null,
                    };
                    setClarifications((prev) => {
                      if (prev.some((c) => c.id === row.id)) return prev;
                      return [...prev, row];
                    });
                    setActiveClarificationId((prev) => prev ?? row.id);
                    break;
                  }
                  case 'gem_files_found':
                    if (data.count) {
                      setDocsCount((prev) => prev + data.count);
                      setNewDocsAlert(true);
                    }
                    break;
                  case 'suggestions':
                    if (data.items) setSuggestions(data.items);
                    break;
                  case 'error':
                    setError(data.message || 'Agent error');
                    break;
                  case 'agent_warning':
                  case 'agent_error': {
                    // Typed reliability events from the costing agent (and
                    // other agents over time). Accumulated and attached to
                    // the assistant message so the renderer can show them
                    // as distinct bubbles — NEVER concatenated into the
                    // streaming text (that was the line-884 bug).
                    const notice: AgentNotice = {
                      severity:
                        eventType === 'agent_error' ? 'error' : 'warning',
                      code: String(data.code || 'unknown'),
                      message: String(data.message || ''),
                      details: data.details,
                      agent_key: data.agent_key,
                    };
                    currentAgentNotices = [...currentAgentNotices, notice];
                    break;
                  }
                  case 'done':
                    currentOutputType = data.output_type || currentOutputType;
                    if (data.agents_used) currentRoutedFrom = data.agents_used.join(',');
                    if (data.run_cost) currentRunCost = data.run_cost;
                    if (activeSession.id) {
                      getCommandCenterSession(activeSession.id).then((s) => {
                        const newPipeline = s.pipeline_state || {};
                        const newlyCompleted = Object.entries(newPipeline)
                          .filter(([k, v]) => v && !pipelineState[k])
                          .map(([k]) => k);
                        if (newlyCompleted.length > 0) {
                          const allCompleted = Object.entries(newPipeline)
                            .filter(([, v]) => v).map(([k]) => k);
                          setMessages((prev) => [...prev, {
                            id: Date.now() + 2,
                            role: 'system',
                            content: '__pipeline_update__',
                            metadata: { completedSteps: allCompleted },
                            created_at: new Date().toISOString(),
                          } as ChatMessage]);
                        }
                        setSession(s);
                        setPipelineState(newPipeline);
                      });
                      // refresh the session list so sidebar counts stay accurate
                      refreshSessionList().catch(() => {
                        // non-fatal; sidebar just shows stale counts until next refresh
                      });
                    }
                    break;
                  case 'title_updated':
                    if (data.title) {
                      setSession((prev: any) => prev ? { ...prev, title: data.title } : prev);
                    }
                    break;
                  case 'run_started':
                    // Wrapper event from the durable run path — id already saved above.
                    break;
                  case 'run_done':
                    // Worker has finished. Clear the resume pointer; the assistant
                    // message is appended by the post-loop block below.
                    clearActiveRun(activeSession.id);
                    break;
                }
              } catch (parseErr) {
                // Malformed SSE chunk. Previously this fell through and
                // appended the raw dataString to fullText, which is how
                // backend error payloads ended up rendered as agent text
                // inside the chat bubble (see plan: now-i-need-to-
                // synchronous-taco.md root cause #4). Log + discard.
                // Backend errors come through `agent_error` SSE events
                // now, NOT as text concatenated into the response.
                // eslint-disable-next-line no-console
                console.warn(
                  '[SSE] dropped malformed chunk',
                  { eventType, parseErr, dataStrPreview: dataStr.slice(0, 200) }
                );
              }
              eventType = '';
            }
          }
        }
      }

      if (fullText || currentPendingPlan || currentPlanProgress || currentAgentNotices.length > 0) {
        const assistantMsg: ChatMessage = {
          id: Date.now() + 1,
          role: 'assistant',
          content: fullText,
          output_type: currentOutputType,
          routed_from: currentRoutedFrom,
          metadata: currentRunCost ? { run_cost: currentRunCost } : undefined,
          created_at: new Date().toISOString(),
          artifact_refs: currentArtifactRefs.length > 0 ? currentArtifactRefs : undefined,
          timeline: currentTimeline.length > 0 ? currentTimeline : undefined,
          timelineBudget: currentBudget,
          pendingPlan: currentPendingPlan || undefined,
          planProgress: currentPlanProgress || undefined,
          agent_notices: currentAgentNotices.length > 0 ? currentAgentNotices : undefined,
        };
        setMessages((prev) => [...prev, assistantMsg]);
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
        if (streamingText) {
          setMessages((prev) => [...prev, {
            id: Date.now() + 1,
            role: 'assistant',
            content: streamingText + '\n\n*[Generation stopped]*',
            created_at: new Date().toISOString(),
            artifact_refs: currentArtifactRefs.length > 0 ? currentArtifactRefs : undefined,
            timeline: currentTimeline.length > 0 ? currentTimeline : undefined,
            timelineBudget: currentBudget,
          pendingPlan: currentPendingPlan || undefined,
          planProgress: currentPlanProgress || undefined,
          }]);
        }
        // User hit Stop — drop the resume pointer so we don't replay on reload.
        clearActiveRun(activeSession.id);
      } else {
        console.error('Command Center chat error:', err);
        // "Failed to fetch" surfaces when the SSE connection drops mid-stream
        // (long analyzer runs, brief network blip, browser idle timeout).
        // The backend run is durable — it keeps writing to the AgentExecution
        // row even when the client disconnects. So we keep the saved runId
        // and tell the user what to do, rather than the cryptic "Failed to fetch".
        const isNetworkFailure =
          err.name === 'TypeError' ||
          /failed to fetch|network|load failed|connection/i.test(err.message || '');
        const activeRun = loadActiveRun(activeSession.id);
        if (isNetworkFailure && activeRun) {
          // The run is durable — it keeps going and still writes its reply.
          // Reattach for the user instead of asking them to refresh: telling
          // someone to babysit a page is the behaviour this is meant to end.
          // One attempt, not a loop; if it cannot reattach, say so plainly.
          //
          // Awaited, not fired and forgotten: this handler's `finally` tears
          // down the streaming state, and an unawaited reattach would have it
          // pulled out from under the reattached stream the moment it
          // suspended — tokens arriving into a UI that had stopped showing
          // them.
          setError(
            'Connection lost — reconnecting to the run still going in the background…'
          );
          await reattachAfterDrop(activeSession.id, activeRun.run_id);
        } else {
          setError(err.message || 'Failed to get AI response.');
          clearActiveRun(activeSession.id);
        }
      }
    } finally {
      setStreaming(false);
      setStreamingText('');
      setStreamingTimeline([]);
      setStreamingBudget(undefined);
    setStreamingPlanProgress(null);
      setRoutingInfo(null);
      setReliabilityStatus(null);
      setAgentStatus(null);
      setAbortController(null);
      activeRunIdRef.current = null;
      isSendingRef.current = false;
    }
  };


  /** Stop the run, not just the listening.
   *
   * Aborting the fetch was the whole of the old Stop button: the browser went
   * quiet and the worker carried on to completion, still spending the user's
   * budget on work they had asked it to stop. Tell the backend first, then
   * abort locally — in that order, because if the request fails we have not
   * yet thrown away the stream that is our only view of the run.
   */
  const stopGeneration = async () => {
    const runId = activeRunIdRef.current;
    if (!runId) {
      abortController?.abort();
      return;
    }
    setStopping(true);
    try {
      await cancelRun(runId);
    } catch (err) {
      console.warn('cancelRun failed; stopping the local stream anyway:', err);
    } finally {
      setStopping(false);
      abortController?.abort();
    }
  };

  // --- Decision Maker plan approval ---
  //
  // Called by DecisionPlanCard when the user clicks Approve / Change / Deny.
  // POSTs to /sessions/{id}/decision/respond, then consumes the returned SSE
  // stream using the same parser shape as sendMessage so the live timeline
  // and tokens render identically.
  const handlePlanResponse = async (
    messageId: number,
    action: 'approve' | 'change' | 'deny' | 'next',
    feedback?: string,
  ) => {
    if (!session || streaming) return;

    // Mark the card as decided so buttons disable immediately. "next" is a
    // silent advance — no card state change.
    if (action !== 'next') {
      setMessages((prev) => prev.map((m) =>
        m.id === messageId ? { ...m, planDecision: action as 'approve' | 'change' | 'deny' } : m,
      ));
    }

    const controller = new AbortController();
    setAbortController(controller);
    setStreaming(true);
    setStreamingText('');
    setStreamingTimeline([]);
    setStreamingBudget(undefined);
    setError('');

    let fullText = '';
    let currentOutputType = 'general';
    let currentRoutedFrom = '';
    let currentRunCost: RunCost | null = null;
    let currentArtifactRefs: ArtifactCreatedEvent[] = [];
    let currentTimeline: TimelineEvent[] = [];
    let currentBudget: TimelineBudget | undefined = undefined;
    let currentPendingPlan: PendingPlan | null = null;
    let currentPlanProgress: { title?: string | null; steps: PlanProgressStep[] } | null = null;

    try {
      const response = await respondToDecisionPlan(session.id, action, feedback, controller.signal);
      if (!response.ok) {
        throw new Error(`Plan response failed (${response.status})`);
      }
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          let eventType = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              const dataStr = line.slice(6);
              try {
                const data = JSON.parse(dataStr);
                switch (eventType) {
                  case 'agent_start':
                    setRoutingInfo({
                      intent: 'decision_maker',
                      agents: [data.agent_key || 'decision_maker'],
                      agentNames: [data.display_name || 'Decision Maker'],
                    });
                    break;
                  case 'assistant_continuing':
                  case 'assistant_resumed':
                  case 'assistant_retried_empty':
                  case 'assistant_truncated':
                  case 'assistant_empty_content':
                  case 'assistant_retried_no_thinking':
                  case 'prerequisite_running':
                  case 'prerequisite_complete':
                  case 'prerequisite_failed':
                  case 'analyzer_progress':
                  case 'costing_batch_progress':
                  case 'costing_boq_extract':
                  case 'tender_enriched':
                    handleReliabilityEvent(eventType, data);
                    break;
                  case 'token':
                    if (data.content) {
                      fullText += data.content;
                      setStreamingText(fullText);
                      // First real token after a continuation/retry means
                      // we've successfully resumed — clear any lingering
                      // indicator so the UI doesn't keep showing it.
                      setReliabilityStatus((prev) =>
                        prev && prev.kind !== 'resumed' ? null : prev
                      );
                      // Tokens are arriving — the agent is no longer
                      // "thinking" or "running a tool"; clear the status pill.
                      setAgentStatus(null);
                    }
                    break;
                  case 'token_reset':
                    // A tool-using agent narrates on its way to a tool call
                    // ("Let me search for that"). That text is not the answer,
                    // and this accumulator is what gets saved as the message.
                    fullText = '';
                    setStreamingText('');
                    break;
                  case 'agent_status': {
                    const phase = data.phase as string;
                    const runId = String(data.run_id ?? '');
                    if (phase === 'tool_done') {
                      setAgentStatus((prev) =>
                        prev && prev.runId === runId ? null : prev
                      );
                    } else if (phase === 'tool_running' || phase === 'llm_thinking') {
                      const startedAtMs =
                        typeof data.started_at_ms === 'number'
                          ? data.started_at_ms
                          : Date.now();
                      setAgentStatus({
                        runId,
                        message: data.message || 'Working',
                        startedAtMs,
                      });
                    }
                    break;
                  }
                  case 'agent_complete':
                    currentOutputType = data.output_type || 'general';
                    currentRoutedFrom = data.agent_key || '';
                    if (Array.isArray(data.trace)) currentTimeline = data.trace as TimelineEvent[];
                    if (data.pending_plan) currentPendingPlan = data.pending_plan as PendingPlan;
                    break;
                  case 'plan_proposed':
                    currentPendingPlan = {
                      title: data.title || 'Proposed plan',
                      reasoning: data.reasoning || '',
                      steps: Array.isArray(data.steps) ? data.steps : [],
                    };
                    break;
                  case 'plan_execution_started':
                    currentPlanProgress = {
                      title: data.title || null,
                      steps: [],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  case 'plan_step_progress': {
                    const next: PlanProgressStep = {
                      index: data.index ?? 0,
                      total: data.total ?? 0,
                      agent: data.agent ?? null,
                      agent_display_name: data.agent_display_name ?? null,
                      description: data.description ?? '',
                      status: (data.status as PlanProgressStep['status']) || 'running',
                      error: data.error ?? null,
                    };
                    if (!currentPlanProgress) {
                      currentPlanProgress = { title: null, steps: [] };
                    }
                    const existingIdx = currentPlanProgress.steps.findIndex((s) => s.index === next.index);
                    if (existingIdx >= 0) {
                      currentPlanProgress.steps[existingIdx] = next;
                    } else {
                      currentPlanProgress.steps = [...currentPlanProgress.steps, next];
                    }
                    currentPlanProgress = {
                      ...currentPlanProgress,
                      steps: [...currentPlanProgress.steps],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  }
                  case 'plan_execution_complete':
                    break;
                  case 'workspace_initialized':
                    // Non-fatal notification — frontend can refresh its
                    // workspace view later. No UI change required here.
                    break;
                  case 'artifact_created': {
                    const evt = data as ArtifactCreatedEvent;
                    currentArtifactRefs = [...currentArtifactRefs, evt];
                    getSessionArtifacts(session.id).then((arts) => {
                      setArtifacts(arts);
                      setSelectedArtifactId(evt.artifact_id);
                      if (!userClosedPanel.current) setArtifactPanelOpen(true);
                    });
                    break;
                  }
                  case 'decision_thought': {
                    const ev: TimelineEvent = { step: data.step ?? 0, type: 'thought', content: data.content || '' };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_action': {
                    const ev: TimelineEvent = { step: data.step ?? 0, type: 'action', tool: data.tool, input_preview: data.input_preview };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_observation': {
                    const ev: TimelineEvent = { step: data.step ?? 0, type: 'observation', result_preview: data.result_preview, is_error: !!data.is_error };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_budget':
                    currentBudget = {
                      iteration: data.iteration ?? 0,
                      max_iterations: data.max_iterations ?? 15,
                      elapsed_s: data.elapsed_s ?? 0,
                      max_execution_time: data.max_execution_time,
                    };
                    setStreamingBudget(currentBudget);
                    break;
                  case 'error':
                    setError(data.message || 'Agent error');
                    break;
                  case 'done':
                    if (data.output_type) currentOutputType = data.output_type;
                    // refresh the session list so sidebar counts stay accurate
                    refreshSessionList().catch(() => {
                      // non-fatal; sidebar just shows stale counts until next refresh
                    });
                    break;
                }
              } catch { /* ignore partial data */ }
              eventType = '';
            }
          }
        }
      }

      if (fullText || currentPendingPlan || currentPlanProgress) {
        const assistantMsg: ChatMessage = {
          id: Date.now() + 1,
          role: 'assistant',
          content: fullText,
          output_type: currentOutputType,
          routed_from: currentRoutedFrom,
          metadata: currentRunCost ? { run_cost: currentRunCost } : undefined,
          created_at: new Date().toISOString(),
          artifact_refs: currentArtifactRefs.length > 0 ? currentArtifactRefs : undefined,
          timeline: currentTimeline.length > 0 ? currentTimeline : undefined,
          timelineBudget: currentBudget,
          pendingPlan: currentPendingPlan || undefined,
          planProgress: currentPlanProgress || undefined,
        };
        setMessages((prev) => [...prev, assistantMsg]);
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        console.error('Plan response error:', err);
        setError(err.message || 'Failed to submit plan response.');
      }
    } finally {
      setStreaming(false);
      setStreamingText('');
      setStreamingTimeline([]);
      setStreamingBudget(undefined);
    setStreamingPlanProgress(null);
      setRoutingInfo(null);
      setReliabilityStatus(null);
      setAgentStatus(null);
      setAbortController(null);
    }
  };

  // --- Reattach after the connection drops mid-stream ---
  //
  // The SSE connection is not the run. When it dies — a network blip, a laptop
  // lid, a proxy idle timeout — the run carries on in the worker and its reply
  // is still written. Previously the user was told to refresh the page by
  // hand, which is precisely the babysitting this is supposed to remove.
  //
  // A long costing outlives whatever sits between the browser and the worker
  // (a proxy idle limit, a laptop lid, a flaky link); the run itself is
  // durable and the events stream replays from Redis. So a dropped stream is
  // reattached, and reattached again if it drops again, with a short backoff,
  // until the run reports done or the run is no longer in flight. One attempt
  // used to be the rule, and a fifteen-minute costing ended on screen as
  // "reopen this session in a minute" while the worker was still writing.
  const REATTACH_DELAYS_MS = [2000, 4000, 8000, 15000, 30000, 30000, 30000, 30000];
  const reattachAfterDrop = async (sessionId: number, runId: string) => {
    for (let attempt = 0; attempt < REATTACH_DELAYS_MS.length; attempt++) {
      const attached = await resumeActiveRun(sessionId, runId);
      if (attached) {
        setError('');
        return;
      }
      // resumeActiveRun clears the pointer when the backend no longer knows
      // the run (finished, 404) -- nothing to reattach to, and the reply is
      // in the session history.
      if (!loadActiveRun(sessionId)) {
        setError('');
        return;
      }
      setError(
        `Connection lost — reconnecting to the run still going in the background (attempt ${attempt + 2})…`,
      );
      await new Promise((resolve) => setTimeout(resolve, REATTACH_DELAYS_MS[attempt]));
    }
    setError(
      'Connection lost. The run is still going in the background — reopen ' +
      'this session in a minute to see the result.',
    );
  };

  // --- Resume an in-flight run from localStorage ---
  //
  // Called on session mount when an active run pointer exists. Re-opens the
  // Redis-backed SSE stream and feeds it into the same parse/accumulate logic
  // as sendMessage. Returns silently when the run is already finished, 404s,
  // or the stream errors — the stored pointer is cleared either way.
  //
  // TODO: DRY up with sendMessage's parser — they differ only in the setup
  // (user message + file upload).
  //
  // Returns whether it actually attached, so a caller can tell the difference
  // between "reconnected, watch this space" and "there was nothing there" —
  // reporting the first when the second happened is how a user ends up
  // waiting on a screen that will never change.
  const resumeActiveRun = async (sessionId: number, runId: string): Promise<boolean> => {
    let attached = false;
    const controller = new AbortController();
    // Stop has to reach a run we reattached to as well as one we started. This
    // is the case the durability work created: the user comes back on another
    // device to a job already in flight, and "stop it" is the first thing they
    // are likely to want.
    activeRunIdRef.current = runId;
    setAbortController(controller);
    setStreaming(true);
    setStreamingText('');
    setStreamingTimeline([]);
    setStreamingBudget(undefined);

    let fullText = '';
    let currentOutputType = 'general';
    let currentRoutedFrom = '';
    let currentRunCost: RunCost | null = null;
    let currentArtifactRefs: ArtifactCreatedEvent[] = [];
    let currentTimeline: TimelineEvent[] = [];
    let currentBudget: TimelineBudget | undefined = undefined;
    let currentPendingPlan: PendingPlan | null = null;
    let currentPlanProgress: { title?: string | null; steps: PlanProgressStep[] } | null = null;

    try {
      const response = await openRunStream(runId, undefined, controller.signal);
      if (!response.ok) {
        clearActiveRun(sessionId);
        return false;
      }
      attached = true;

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          let eventType = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              const dataStr = line.slice(6);
              try {
                const data = JSON.parse(dataStr);
                switch (eventType) {
                  case 'session':
                    break;
                  case 'routing':
                    setRoutingInfo({
                      intent: data.intent,
                      agents: data.agents || [],
                      agentNames: data.agent_names || [],
                      reasoning: data.reasoning || undefined,
                    });
                    break;
                  case 'agent_start':
                    setRoutingInfo((prev) => ({
                      ...prev!,
                      intent: prev?.intent || '',
                      agents: prev?.agents || [data.agent_key],
                      agentNames: [data.display_name || data.agent_key],
                    }));
                    break;
                  case 'assistant_continuing':
                  case 'assistant_resumed':
                  case 'assistant_retried_empty':
                  case 'assistant_truncated':
                  case 'assistant_empty_content':
                  case 'assistant_retried_no_thinking':
                  case 'prerequisite_running':
                  case 'prerequisite_complete':
                  case 'prerequisite_failed':
                  case 'analyzer_progress':
                  case 'costing_batch_progress':
                  case 'costing_boq_extract':
                  case 'tender_enriched':
                    handleReliabilityEvent(eventType, data);
                    break;
                  case 'token':
                    if (data.content) {
                      fullText += data.content;
                      setStreamingText(fullText);
                      // First real token after a continuation/retry means
                      // we've successfully resumed — clear any lingering
                      // indicator so the UI doesn't keep showing it.
                      setReliabilityStatus((prev) =>
                        prev && prev.kind !== 'resumed' ? null : prev
                      );
                      // Tokens are arriving — the agent is no longer
                      // "thinking" or "running a tool"; clear the status pill.
                      setAgentStatus(null);
                    }
                    break;
                  case 'token_reset':
                    // A tool-using agent narrates on its way to a tool call
                    // ("Let me search for that"). That text is not the answer,
                    // and this accumulator is what gets saved as the message.
                    fullText = '';
                    setStreamingText('');
                    break;
                  case 'agent_status': {
                    const phase = data.phase as string;
                    const runId = String(data.run_id ?? '');
                    if (phase === 'tool_done') {
                      setAgentStatus((prev) =>
                        prev && prev.runId === runId ? null : prev
                      );
                    } else if (phase === 'tool_running' || phase === 'llm_thinking') {
                      const startedAtMs =
                        typeof data.started_at_ms === 'number'
                          ? data.started_at_ms
                          : Date.now();
                      setAgentStatus({
                        runId,
                        message: data.message || 'Working',
                        startedAtMs,
                      });
                    }
                    break;
                  }
                  case 'agent_complete':
                    currentOutputType = data.output_type || 'general';
                    currentRoutedFrom = data.agent_key || '';
                    if (Array.isArray(data.trace)) {
                      currentTimeline = data.trace as TimelineEvent[];
                      setStreamingTimeline(currentTimeline);
                    }
                    if (data.pending_plan) {
                      currentPendingPlan = data.pending_plan as PendingPlan;
                    }
                    break;
                  case 'plan_proposed':
                    currentPendingPlan = {
                      title: data.title || 'Proposed plan',
                      reasoning: data.reasoning || '',
                      steps: Array.isArray(data.steps) ? data.steps : [],
                    };
                    break;
                  case 'plan_execution_started':
                    currentPlanProgress = {
                      title: data.title || null,
                      steps: [],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  case 'plan_step_progress': {
                    const next: PlanProgressStep = {
                      index: data.index ?? 0,
                      total: data.total ?? 0,
                      agent: data.agent ?? null,
                      agent_display_name: data.agent_display_name ?? null,
                      description: data.description ?? '',
                      status: (data.status as PlanProgressStep['status']) || 'running',
                      error: data.error ?? null,
                    };
                    if (!currentPlanProgress) {
                      currentPlanProgress = { title: null, steps: [] };
                    }
                    const existingIdx = currentPlanProgress.steps.findIndex((s) => s.index === next.index);
                    if (existingIdx >= 0) {
                      currentPlanProgress.steps[existingIdx] = next;
                    } else {
                      currentPlanProgress.steps = [...currentPlanProgress.steps, next];
                    }
                    currentPlanProgress = {
                      ...currentPlanProgress,
                      steps: [...currentPlanProgress.steps],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  }
                  case 'plan_execution_complete':
                    break;
                  case 'workspace_initialized':
                    // Non-fatal notification — frontend can refresh its
                    // workspace view later. No UI change required here.
                    break;
                  case 'decision_thought': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'thought', content: data.content || '',
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_action': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'action',
                      tool: data.tool, input_preview: data.input_preview,
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_observation': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'observation',
                      result_preview: data.result_preview, is_error: !!data.is_error,
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_budget':
                    currentBudget = {
                      iteration: data.iteration ?? 0,
                      max_iterations: data.max_iterations ?? 15,
                      elapsed_s: data.elapsed_s ?? 0,
                      max_execution_time: data.max_execution_time,
                    };
                    setStreamingBudget(currentBudget);
                    break;
                  case 'decision_final':
                    break;
                  case 'artifact_created': {
                    const evt = data as ArtifactCreatedEvent;
                    currentArtifactRefs = [...currentArtifactRefs, evt];
                    getSessionArtifacts(sessionId).then((arts) => {
                      setArtifacts(arts);
                      setSelectedArtifactId(evt.artifact_id);
                      if (!userClosedPanel.current) setArtifactPanelOpen(true);
                    });
                    break;
                  }
                  case 'error':
                    setError(data.message || 'Agent error');
                    break;
                  case 'done':
                    currentOutputType = data.output_type || currentOutputType;
                    if (data.agents_used) currentRoutedFrom = data.agents_used.join(',');
                    if (data.run_cost) currentRunCost = data.run_cost;
                    getCommandCenterSession(sessionId).then((s) => {
                      setSession(s);
                      setPipelineState(s.pipeline_state || {});
                    });
                    // refresh the session list so sidebar counts stay accurate
                    refreshSessionList().catch(() => {
                      // non-fatal; sidebar just shows stale counts until next refresh
                    });
                    break;
                  case 'run_done':
                    clearActiveRun(sessionId);
                    break;
                }
              } catch {
                // Malformed SSE chunk — discard rather than append the raw
                // data string to the response (appending leaks backend error
                // payloads into the agent's visible text). Matches the
                // sendMessage parser's handling.
                console.warn('[resumeActiveRun] skipped unparseable SSE chunk');
              }
              eventType = '';
            }
          }
        }
      }

      if (fullText || currentPendingPlan || currentPlanProgress) {
        setMessages((prev) => [...prev, {
          id: Date.now(),
          role: 'assistant',
          content: fullText,
          output_type: currentOutputType,
          routed_from: currentRoutedFrom,
          metadata: currentRunCost ? { run_cost: currentRunCost } : undefined,
          created_at: new Date().toISOString(),
          artifact_refs: currentArtifactRefs.length > 0 ? currentArtifactRefs : undefined,
          timeline: currentTimeline.length > 0 ? currentTimeline : undefined,
          timelineBudget: currentBudget,
          pendingPlan: currentPendingPlan || undefined,
          planProgress: currentPlanProgress || undefined,
        } as ChatMessage]);
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        console.warn('resumeActiveRun:', err);
      }
      // A network drop mid-stream keeps the pointer: the run is still going
      // and the caller reattaches. Anything else (or a Stop) clears it.
      const networkDrop =
        err?.name === 'TypeError' || /failed to fetch|network|load failed|connection/i.test(err?.message || '');
      if (!networkDrop || err?.name === 'AbortError') {
        clearActiveRun(sessionId);
      }
      attached = false;
    } finally {
      setStreaming(false);
      setStreamingText('');
      setStreamingTimeline([]);
      setStreamingBudget(undefined);
    setStreamingPlanProgress(null);
      setRoutingInfo(null);
      setReliabilityStatus(null);
      setAgentStatus(null);
      setAbortController(null);
      activeRunIdRef.current = null;
    }
    return attached;
  };

  // --- Clarification answer flow ---
  const handleClarificationSubmit = async (answer: string) => {
    if (!session || activeClarificationId == null || clarificationSubmitting) return;
    const clar = clarifications.find((c) => c.id === activeClarificationId);
    if (!clar) return;

    setClarificationSubmitting(true);
    const controller = new AbortController();
    setAbortController(controller);

    // Echo the user's answer into the transcript.
    setMessages((prev) => [...prev, {
      id: Date.now(),
      role: 'user',
      content: `${clar.question}\n\n**Answer:** ${answer}`,
      created_at: new Date().toISOString(),
    } as ChatMessage]);

    // Mark the clarification answered locally & close the modal.
    setClarifications((prev) => prev.map((c) =>
      c.id === clar.id ? { ...c, status: 'answered', answer } : c,
    ));
    setActiveClarificationId(null);

    setStreaming(true);
    setStreamingText('');
    setStreamingTimeline([]);
    setStreamingBudget(undefined);
    setRoutingInfo(null);

    let fullText = '';
    let currentOutputType = 'general';
    let currentRoutedFrom = '';
    let currentRunCost: RunCost | null = null;
    let currentArtifactRefs: ArtifactCreatedEvent[] = [];
    let currentTimeline: TimelineEvent[] = [];
    let currentBudget: TimelineBudget | undefined = undefined;
    let currentPendingPlan: PendingPlan | null = null;
    let currentPlanProgress: { title?: string | null; steps: PlanProgressStep[] } | null = null;

    try {
      const response = await answerClarificationStream(
        session.id, clar.id, answer, controller.signal,
      );
      if (!response.ok) {
        const errBody = await response.text().catch(() => '');
        throw new Error(`HTTP ${response.status}: ${errBody}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          let eventType = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                switch (eventType) {
                  case 'routing':
                    setRoutingInfo({
                      intent: data.intent,
                      agents: data.agents || [],
                      agentNames: data.agent_names || [],
                      reasoning: data.reasoning || undefined,
                    });
                    break;
                  case 'agent_start':
                    setRoutingInfo((prev) => ({
                      ...prev!,
                      intent: prev?.intent || '',
                      agents: prev?.agents || [data.agent_key],
                      agentNames: [data.display_name || data.agent_key],
                    }));
                    break;
                  case 'assistant_continuing':
                  case 'assistant_resumed':
                  case 'assistant_retried_empty':
                  case 'assistant_truncated':
                  case 'assistant_empty_content':
                  case 'assistant_retried_no_thinking':
                  case 'prerequisite_running':
                  case 'prerequisite_complete':
                  case 'prerequisite_failed':
                  case 'analyzer_progress':
                  case 'costing_batch_progress':
                  case 'costing_boq_extract':
                  case 'tender_enriched':
                    handleReliabilityEvent(eventType, data);
                    break;
                  case 'token':
                    if (data.content) {
                      fullText += data.content;
                      setStreamingText(fullText);
                      // First real token after a continuation/retry means
                      // we've successfully resumed — clear any lingering
                      // indicator so the UI doesn't keep showing it.
                      setReliabilityStatus((prev) =>
                        prev && prev.kind !== 'resumed' ? null : prev
                      );
                      // Tokens are arriving — the agent is no longer
                      // "thinking" or "running a tool"; clear the status pill.
                      setAgentStatus(null);
                    }
                    break;
                  case 'token_reset':
                    // A tool-using agent narrates on its way to a tool call
                    // ("Let me search for that"). That text is not the answer,
                    // and this accumulator is what gets saved as the message.
                    fullText = '';
                    setStreamingText('');
                    break;
                  case 'agent_status': {
                    const phase = data.phase as string;
                    const runId = String(data.run_id ?? '');
                    if (phase === 'tool_done') {
                      setAgentStatus((prev) =>
                        prev && prev.runId === runId ? null : prev
                      );
                    } else if (phase === 'tool_running' || phase === 'llm_thinking') {
                      const startedAtMs =
                        typeof data.started_at_ms === 'number'
                          ? data.started_at_ms
                          : Date.now();
                      setAgentStatus({
                        runId,
                        message: data.message || 'Working',
                        startedAtMs,
                      });
                    }
                    break;
                  }
                  case 'agent_complete':
                    currentOutputType = data.output_type || 'general';
                    currentRoutedFrom = data.agent_key || '';
                    if (Array.isArray(data.trace)) {
                      currentTimeline = data.trace as TimelineEvent[];
                      setStreamingTimeline(currentTimeline);
                    }
                    if (data.pending_plan) {
                      currentPendingPlan = data.pending_plan as PendingPlan;
                    }
                    break;
                  case 'plan_proposed':
                    currentPendingPlan = {
                      title: data.title || 'Proposed plan',
                      reasoning: data.reasoning || '',
                      steps: Array.isArray(data.steps) ? data.steps : [],
                    };
                    break;
                  case 'plan_execution_started':
                    currentPlanProgress = {
                      title: data.title || null,
                      steps: [],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  case 'plan_step_progress': {
                    const next: PlanProgressStep = {
                      index: data.index ?? 0,
                      total: data.total ?? 0,
                      agent: data.agent ?? null,
                      agent_display_name: data.agent_display_name ?? null,
                      description: data.description ?? '',
                      status: (data.status as PlanProgressStep['status']) || 'running',
                      error: data.error ?? null,
                    };
                    if (!currentPlanProgress) {
                      currentPlanProgress = { title: null, steps: [] };
                    }
                    const existingIdx = currentPlanProgress.steps.findIndex((s) => s.index === next.index);
                    if (existingIdx >= 0) {
                      currentPlanProgress.steps[existingIdx] = next;
                    } else {
                      currentPlanProgress.steps = [...currentPlanProgress.steps, next];
                    }
                    currentPlanProgress = {
                      ...currentPlanProgress,
                      steps: [...currentPlanProgress.steps],
                    };
                    setStreamingPlanProgress(currentPlanProgress);
                    break;
                  }
                  case 'plan_execution_complete':
                    break;
                  case 'workspace_initialized':
                    // Non-fatal notification — frontend can refresh its
                    // workspace view later. No UI change required here.
                    break;
                  case 'decision_thought': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'thought', content: data.content || '',
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_action': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'action',
                      tool: data.tool, input_preview: data.input_preview,
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_observation': {
                    const ev: TimelineEvent = {
                      step: data.step ?? 0, type: 'observation',
                      result_preview: data.result_preview, is_error: !!data.is_error,
                    };
                    currentTimeline = [...currentTimeline, ev];
                    setStreamingTimeline(currentTimeline);
                    break;
                  }
                  case 'decision_budget':
                    currentBudget = {
                      iteration: data.iteration ?? 0,
                      max_iterations: data.max_iterations ?? 15,
                      elapsed_s: data.elapsed_s ?? 0,
                      max_execution_time: data.max_execution_time,
                    };
                    setStreamingBudget(currentBudget);
                    break;
                  case 'decision_final':
                    break;
                  case 'artifact_created': {
                    const evt = data as ArtifactCreatedEvent;
                    currentArtifactRefs = [...currentArtifactRefs, evt];
                    getSessionArtifacts(session.id).then((arts) => {
                      setArtifacts(arts);
                      setSelectedArtifactId(evt.artifact_id);
                      if (!userClosedPanel.current) setArtifactPanelOpen(true);
                    });
                    break;
                  }
                  case 'clarification_request': {
                    const row: PendingClarification = {
                      id: data.clarification_id,
                      agent_key: data.agent_key || 'assistant',
                      question: data.question || '',
                      options: data.options || [],
                      context: data.context || {},
                      status: 'pending',
                      answer: null,
                      created_at: new Date().toISOString(),
                      answered_at: null,
                    };
                    setClarifications((prev) =>
                      prev.some((c) => c.id === row.id) ? prev : [...prev, row],
                    );
                    setActiveClarificationId((prev) => prev ?? row.id);
                    break;
                  }
                  case 'suggestions':
                    if (data.items) setSuggestions(data.items);
                    break;
                  case 'error':
                    setError(data.message || 'Agent error');
                    break;
                  case 'done':
                    currentOutputType = data.output_type || currentOutputType;
                    if (data.agents_used) currentRoutedFrom = data.agents_used.join(',');
                    if (data.run_cost) currentRunCost = data.run_cost;
                    // refresh the session list so sidebar counts stay accurate
                    refreshSessionList().catch(() => {
                      // non-fatal; sidebar just shows stale counts until next refresh
                    });
                    break;
                }
              } catch {
                /* ignore parse */
              }
              eventType = '';
            }
          }
        }
      }

      if (fullText || currentPendingPlan || currentPlanProgress) {
        setMessages((prev) => [...prev, {
          id: Date.now() + 1,
          role: 'assistant',
          content: fullText,
          output_type: currentOutputType,
          routed_from: currentRoutedFrom,
          metadata: currentRunCost ? { run_cost: currentRunCost } : undefined,
          created_at: new Date().toISOString(),
          artifact_refs: currentArtifactRefs.length > 0 ? currentArtifactRefs : undefined,
          timeline: currentTimeline.length > 0 ? currentTimeline : undefined,
          timelineBudget: currentBudget,
          pendingPlan: currentPendingPlan || undefined,
          planProgress: currentPlanProgress || undefined,
        } as ChatMessage]);
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        console.error('Clarification resume error:', err);
        setError(err.message || 'Failed to resume after clarification.');
      }
    } finally {
      setStreaming(false);
      setStreamingText('');
      setStreamingTimeline([]);
      setStreamingBudget(undefined);
    setStreamingPlanProgress(null);
      setRoutingInfo(null);
      setReliabilityStatus(null);
      setAgentStatus(null);
      setAbortController(null);
      setClarificationSubmitting(false);
    }
  };

  const handleClarificationCancel = async () => {
    if (!session || activeClarificationId == null) return;
    const id = activeClarificationId;
    setActiveClarificationId(null);
    try {
      await cancelClarification(session.id, id);
    } catch {
      /* non-fatal */
    }
    setClarifications((prev) => prev.map((c) =>
      c.id === id ? { ...c, status: 'cancelled' } : c,
    ));
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  };

  const handleSuggestionSelect = (s: Suggestion) => {
    if (s.action === 'chat') {
      sendMessage(s.text);
    }
  };

  const handleArtifactEdit = async (artifactId: number, content: string) => {
    try {
      await updateArtifact(artifactId, { content });
      const arts = await getSessionArtifacts(session.id);
      setArtifacts(arts);
    } catch {
      setError('DRPL could not save those changes. Please try again.');
    }
  };

  // --- Workspace Init Handler ---
  const handleWorkspaceInit = async () => {
    if (!session) return;
    setWorkspaceInitLoading(true);
    setError('');
    try {
      const result = await initSessionWorkspace(session.id);
      setSession(result.session);
      setPipelineState(result.session.pipeline_state || {});

      if (result.needs_checklist) {
        setError('Prepare a checklist first, then return here to start the document work.');
        setActiveTab('chat');
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'DRPL could not prepare the workspace. Please try again.');
    } finally {
      setWorkspaceInitLoading(false);
    }
  };

  // --- File Attachment Handlers ---
  const ALLOWED_EXTENSIONS = ['.pdf', '.png', '.jpg', '.jpeg', '.gif', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.txt'];

  const addFiles = (files: FileList | File[]) => {
    const valid = Array.from(files).filter((f) => {
      const ext = '.' + f.name.split('.').pop()?.toLowerCase();
      if (!ALLOWED_EXTENSIONS.includes(ext)) return false;
      if (f.size > 20 * 1024 * 1024) return false;
      return true;
    });
    if (valid.length > 0) {
      setPendingFiles((prev) => [...prev, ...valid]);
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) addFiles(e.target.files);
    e.target.value = '';
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    if (e.clipboardData.files.length > 0) {
      e.preventDefault();
      addFiles(e.clipboardData.files);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    if (e.dataTransfer.files.length > 0) {
      addFiles(e.dataTransfer.files);
    }
  };

  const removeFile = (index: number) => {
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleNewSession = () => {
    // ChatGPT-style: "New chat" just opens the empty composer. The session is
    // created lazily on the first message (see sendMessage), so we never leave
    // behind empty "New Session" rows.
    if (routeSessionId || routeTenderId) {
      navigate('/command-center');
    } else {
      // Already on the bare chat route — just reset to a fresh empty state.
      setSession(null);
      setMessages([]);
      setArtifacts([]);
      setSuggestions([]);
      setActiveTab('chat');
      setArtifactPanelOpen(false);
      setArtifactFullScreen(false);
      setInput('');
    }
  };

  // --- Bulk select + delete handlers ---

  const refreshSessionList = async () => {
    try {
      const list = await listCommandCenterSessions();
      setSessionList(list);
    } catch { /* ignore */ }
  };

  const handleSingleDelete = async () => {
    if (!deleteTargetId) return;
    const wasActive = deleteTargetId === session?.id;
    try {
      await deleteCommandCenterSession(deleteTargetId);
      setDeleteTargetId(null);
      await refreshSessionList();
      // If we just deleted the open chat, drop back to a fresh empty composer.
      if (wasActive) navigate('/command-center');
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to delete chat');
    }
  };

  // --- Render ---


  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="animate-spin text-accent" size={32} />
      </div>
    );
  }

  // No session yet = the fresh "new chat" state — the composer must be usable
  // so the first message can lazily create a session.
  const canChat = !session || session.status === 'draft' || session.status === 'revision_requested';
  const hasTender = !!session?.tender_id;
  const completedSteps = Object.entries(pipelineState)
    .filter(([, v]) => v)
    .map(([k]) => k);

  return (
    <div className="flex h-full flex-col bg-background">
      {/* Header */}
      <div className="flex min-h-[68px] flex-shrink-0 items-center justify-between border-b border-border bg-card px-4 py-2.5 lg:px-5">
        <div className="flex items-center gap-3 min-w-0">
          <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-emerald-600 text-white shadow-sm">
            <Sparkles size={15} />
          </div>
          <div className="min-w-0">
            <h2 className="max-w-xl truncate text-sm font-semibold leading-tight text-foreground">
              {session ? cleanSessionTitle(session) : 'New chat'}
            </h2>
            {session?.tender_title && (
              <p className="mt-0.5 max-w-xl truncate text-[11px] text-muted-foreground">Tender workspace · Messages and results are saved automatically</p>
            )}
          </div>
        </div>

        {/* Right side — tab navigation + artifact count */}
        <div className="flex max-w-[65%] flex-shrink-0 items-center gap-1 overflow-x-auto py-1">
          {/* Mobile-only: open palette since sidebar is hidden < 640px */}
          <button
            onClick={() => setPaletteOpen(true)}
            className="sm:hidden flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-card border border-border text-foreground text-xs"
            title="Switch session"
            aria-label="Switch session"
          >
            <Search size={14} />
            Switch
          </button>
          {artifacts.length > 0 && (
            <button
              onClick={() => {
                if (artifactPanelOpen) {
                  userClosedPanel.current = true;
                  setArtifactPanelOpen(false);
                  setArtifactFullScreen(false);
                } else {
                  userClosedPanel.current = false;
                  setArtifactPanelOpen(true);
                  setActiveTab('chat');
                }
              }}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                artifactPanelOpen
                  ? 'bg-accent text-accent-foreground border border-accent'
                  : 'bg-muted text-muted-foreground hover:bg-muted/70 border border-transparent'
              }`}
            >
              <FileText size={13} />
              {artifacts.length} {artifacts.length === 1 ? 'Result' : 'Results'}
            </button>
          )}
          {session && (
            <button
              onClick={() => switchTab('workspace')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                activeTab === 'workspace'
                  ? 'bg-accent/15 text-accent border border-accent/20'
                  : 'bg-muted text-muted-foreground hover:bg-muted/70 border border-transparent'
              }`}
            >
              <LayoutGrid size={13} />
              Preparation
              {pipelineState.workspace_setup && (
                <span className="w-1.5 h-1.5 rounded-full bg-success" />
              )}
            </button>
          )}
          {session?.tender_id && (
            <button
              onClick={() => switchTab('documents')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors relative ${
                activeTab === 'documents'
                  ? 'bg-accent/15 text-accent border border-accent/20'
                  : 'bg-muted text-muted-foreground hover:bg-muted/70 border border-transparent'
              }`}
            >
              <Search size={13} />
              Documents
              {docsCount > 0 && (
                <span className="px-1.5 py-0.5 bg-muted text-muted-foreground text-xs rounded-full font-medium">
                  {docsCount}
                </span>
              )}
              {newDocsAlert && (
                <span className="w-1.5 h-1.5 rounded-full bg-warning" />
              )}
            </button>
          )}
        </div>
      </div>

      {/* ── Main content: horizontal split (chat + artifact panel | workspace | documents) ── */}
      <div className="flex flex-1 overflow-hidden relative">

      {/* NEW: Session sidebar — visible regardless of active tab */}
      <SessionSidebar
        sessions={filteredSessionList}
        currentSessionId={session?.id ?? null}
        onSwitch={(id) => navigate(`/command-center/${id}`)}
        onNew={handleNewSession}
        onDelete={(id) => setDeleteTargetId(id)}
        isLoading={sessionListLoading}
        filter={sessionFilters.filter}
        onFilterChange={sessionFilters.setFilter}
        sort={sessionFilters.sort}
        onSortChange={sessionFilters.setSort}
        activeFilterCount={sessionFilters.activeFilterCount}
        onClearFilters={sessionFilters.clearFilters}
      />

      {/* ── Workspace tab ── */}
      <div className={`flex-1 overflow-y-auto ${activeTab !== 'workspace' ? 'hidden' : ''}`}>
        {session?.tender_id ? (
          <WorkspaceTabContent
            tenderId={session.tender_id}
            onOpenDocument={(itemId) => navigate(`/tenders/${session.tender_id}/workspace/${itemId}`)}
            onOpenAnnexures={(itemId) =>
              navigate(
                `/tenders/${session.tender_id}/workspace/annexures${itemId ? `#annexure-${itemId}` : ''}`,
              )
            }
            compact
          />
        ) : (
          <div className="text-center py-20">
            <Layers size={48} className="mx-auto text-muted-foreground/50 mb-4" />
            <h2 className="text-xl font-bold text-foreground mb-2">Prepare your tender</h2>
            <p className="text-muted-foreground mb-2 max-w-md mx-auto">
              Keep the required documents, tasks, and progress together in one guided workspace.
            </p>
            <p className="text-xs text-muted-foreground mb-6">DRPL will connect this conversation to the tender automatically.</p>
            {error && activeTab === 'workspace' && (
              <div className="mb-4 mx-auto max-w-md px-4 py-2 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg text-sm text-red-600 dark:text-red-400">{error}</div>
            )}
            <button
              onClick={handleWorkspaceInit}
              disabled={workspaceInitLoading}
              className="inline-flex items-center gap-2 rounded-xl bg-emerald-600 px-6 py-3 font-bold text-white hover:-translate-y-px hover:bg-emerald-700 disabled:opacity-50"
            >
              {workspaceInitLoading
                ? <><Loader2 size={18} className="animate-spin" />Preparing...</>
                : <><LayoutGrid size={18} />Start preparing</>
              }
            </button>
          </div>
        )}
      </div>

      {/* ── Documents tab ── */}
      {session?.tender_id && (
        <div className={`flex-1 overflow-hidden ${activeTab !== 'documents' ? 'hidden' : ''}`}>
          <DocumentsTabContent sessionId={session.id} tenderId={session.tender_id} />
        </div>
      )}

      {/* ── Chat tab — chat + side-by-side artifact drawer (Claude-style) ── */}
      <div className={`flex flex-row flex-1 min-w-0 overflow-hidden ${activeTab !== 'chat' ? 'hidden' : ''}`}>

        {/* ── Chat view — stays visible; narrows when the artifact drawer opens.
             On mobile the drawer overlays, so hide the chat underneath it. ── */}
        <div
          className={`relative flex min-h-0 min-w-0 flex-1 flex-col bg-card ${artifactPanelOpen ? 'hidden md:flex' : ''} ${artifactFullScreen ? 'md:hidden' : ''}`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {/* Drag overlay */}
          {isDragOver && (
            <div className="absolute inset-0 z-20 bg-accent/10 border-2 border-dashed border-accent rounded-lg flex items-center justify-center pointer-events-none">
              <div className="text-center">
                <Paperclip size={32} className="mx-auto text-accent mb-2" />
                <p className="text-sm font-medium text-accent">Drop files here</p>
              </div>
            </div>
          )}

          {/* Messages */}
          <div
            ref={chatContainerRef}
            onScroll={handleScroll}
            className="flex-1 overflow-y-auto scroll-smooth"
          >
            <div className="flex flex-col min-h-full">

            {/* Empty state — flex-1 keeps it vertically centered */}
            {messages.length <= 1 && !streaming && (
              <div className="flex flex-1 flex-col items-center justify-center px-5 py-14">
                <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-2xl bg-emerald-600 text-white shadow-sm">
                  <Sparkles size={21} />
                </div>
                <h3 className="mb-2 text-xl font-semibold tracking-tight text-foreground">How can I help with this tender?</h3>
                <p className="mb-8 max-w-md text-center text-sm leading-6 text-muted-foreground">
                  Pick a task to get started, or just type what you need — I can analyze tenders,
                  generate proposals, create checklists, and estimate costs.
                </p>
                <div className="grid w-full max-w-2xl grid-cols-1 gap-2.5 sm:grid-cols-2">
                  {SUGGESTION_PROMPTS.map((s, i) => {
                    const Icon = s.icon;
                    return (
                      <button
                        key={i}
                        onClick={() => sendMessage(s.text)}
                        className="flex items-start gap-3 rounded-2xl border border-border bg-background p-4 text-left transition-all hover:border-emerald-500/30 hover:bg-emerald-500/5 hover:shadow-sm"
                      >
                        <div className="p-2 rounded-lg flex-shrink-0 bg-accent/10 text-accent">
                          <Icon size={16} />
                        </div>
                        <span className="text-sm text-foreground leading-snug">{s.text}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Spacer — pushes messages to the bottom when there are few */}
            {(messages.length > 1 || streaming) && <div className="flex-1" />}

            {/* Message list */}
            <div className="mx-auto w-full max-w-3xl space-y-7 px-5 pb-8 pt-8 sm:px-8">
            {messages
              // Safety: deduplicate by ID in case of optimistic + server race
              .filter((msg, i, arr) => arr.findIndex(m => m.id === msg.id) === i)
              .map((msg, index, visibleMsgs) => {
              // Determine if this message visually groups with the previous one
              const prevMsg = visibleMsgs[index - 1];
              const isGrouped = !!(prevMsg
                && prevMsg.role === msg.role
                && prevMsg.content !== '__pipeline_update__'
                && msg.content !== '__pipeline_update__');
              // Pipeline status card (system message)
              if (msg.content === '__pipeline_update__' && msg.role === 'system') {
                return (
                  <div key={msg.id} className="pt-2 pb-1">
                    <PipelineStatusCard completedSteps={msg.metadata?.completedSteps || []} />
                  </div>
                );
              }
              // Skip system messages
              if (msg.role === 'system') return null;

              // User message — right-aligned subtle pill (ChatGPT style)
              if (msg.role === 'user') {
                return (
                  <div key={msg.id} className="flex justify-end">
                    <div className="max-w-[82%] rounded-3xl rounded-br-lg bg-muted/80 px-4 py-3 text-foreground shadow-sm">
                      <MarkdownMessage content={stripFileMarkers(msg.content)} variant="compact" />
                      {msg.attachments && msg.attachments.length > 0 && (
                        <div className="mt-3 grid gap-2 sm:grid-cols-2">
                          {msg.attachments.map((att, i) => (
                            <AttachmentPreview key={att.id ?? i} attachment={att} />
                          ))}
                        </div>
                      )}
                      {messageTime(msg.created_at) && <p className="mt-2 text-right text-[10px] text-muted-foreground">{messageTime(msg.created_at)}</p>}
                    </div>
                  </div>
                );
              }

              // Assistant message — full-width, no bubble, avatar + badge header
              const clean = stripFileMarkers(msg.content);
              const linkedArtifactId = msg.artifact_refs?.[0]?.artifact_id ?? null;
              const linkedArtifactTitle = msg.artifact_refs?.[0]?.title ?? null;
              const showHeader = !isGrouped;

              return (
                <article key={msg.id} className="group">
                  {showHeader && (
                    <div className="mb-3 flex items-center gap-2.5">
                      <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-emerald-600 text-white shadow-sm">
                        <Sparkles size={12} />
                      </div>
                      <span className="text-xs font-semibold text-foreground">DRPL Assistant</span>
                      {messageTime(msg.created_at) && <span className="text-[10px] text-muted-foreground">{messageTime(msg.created_at)}</span>}
                    </div>
                  )}
                  <div className={`${showHeader ? 'pl-10' : 'pl-10 mt-1'} text-[14px] leading-6`}>
                    {/* Decision Maker reasoning timeline — hidden from users.
                        The agent's raw chain-of-thought (thoughts / actions /
                        observations) is internal noise and confuses people.
                        Plan progress now uses the dedicated PlanExecutionProgress
                        component below. */}
                    {msg.pendingPlan ? (
                      <DecisionPlanCard
                        plan={msg.pendingPlan}
                        disabled={!!msg.planDecision}
                        onRespond={(action, feedback) => handlePlanResponse(msg.id!, action, feedback)}
                      />
                    ) : (
                      <>
                        {msg.planProgress && msg.planProgress.steps.length > 0 && (
                          <PlanExecutionProgress
                            title={msg.planProgress.title}
                            steps={msg.planProgress.steps}
                          />
                        )}
                        {/* Full inline render with a soft "Show more" for very
                            long replies (replaces the old hard 700-char cut).
                            Real artifacts still open in the side panel below. */}
                        <CollapsibleMarkdown content={clean} variant="compact" />
                      </>
                    )}
                    {linkedArtifactId && (
                      <div className="mt-3 flex items-center gap-2 flex-wrap animate-fade-slide-up">
                        <button
                          onClick={() => {
                            if (linkedArtifactId) setSelectedArtifactId(linkedArtifactId);
                            userClosedPanel.current = false;
                            setArtifactPanelOpen(true);
                          }}
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-muted hover:bg-muted/70 active:scale-95 text-foreground border border-border rounded-lg text-xs font-medium transition-all duration-150"
                        >
                          <FileText size={12} />
                          {linkedArtifactTitle ? `View: ${linkedArtifactTitle}` : 'Open in side panel'}
                        </button>
                      </div>
                    )}
                    {msg.output_type === 'workspace_operations' && session?.tender_id && (
                      <button
                        onClick={() => switchTab('workspace')}
                        className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-accent bg-accent/10 hover:bg-accent/20 border border-accent/20 px-3 py-1.5 rounded-lg transition-colors"
                      >
                        <Layers size={12} />
                        Open preparation
                      </button>
                    )}
                    {/* Inline artifact cards */}
                    {msg.artifact_refs && msg.artifact_refs.length > 0 && (
                      <div className="mt-2 space-y-1.5 animate-fade-in">
                        {msg.artifact_refs.map((ref) => (
                          <ArtifactCard
                            key={ref.artifact_id}
                            artifactRef={ref}
                            onOpen={(id) => {
                              setSelectedArtifactId(id);
                              userClosedPanel.current = false;
                              setArtifactPanelOpen(true);
                              setActiveTab('chat');
                            }}
                          />
                        ))}
                      </div>
                    )}
                    {/* Typed agent_warning / agent_error bubbles — never
                        concatenated into the response text (the line-884
                        bug). See plan: now-i-need-to-synchronous-taco.md */}
                    {msg.agent_notices && msg.agent_notices.length > 0 && (
                      <div className="mt-2 animate-fade-in">
                        <AgentNoticeList notices={msg.agent_notices} />
                      </div>
                    )}
                    <RunCostLine cost={msg.metadata?.run_cost} />
                  </div>
                </article>
              );
            })}

            {/* Streaming indicator */}
            {streaming && (
              <div className="group rounded-2xl border border-emerald-500/15 bg-emerald-500/[0.03] p-4">
                <div className="mb-3 flex items-center gap-2.5">
                  <div className="flex h-7 w-7 flex-shrink-0 animate-pulse items-center justify-center rounded-full bg-emerald-600 text-white">
                    <Sparkles size={12} />
                  </div>
                  <div>
                    <span className="block text-xs font-semibold text-foreground">DRPL is working</span>
                    <span className="block text-[10px] text-muted-foreground">I’ll show useful progress as it becomes available.</span>
                  </div>
                </div>
                {/* Persistent aria-live region: present for the whole streaming
                    lifecycle (thinking → tokens) so screen readers announce the
                    response as it fills in. */}
                <div className="pl-10" aria-live="polite">
                  {/* Live plan-execution progress (if the decision_maker is
                      executing an approved plan). The raw thought/action
                      timeline is deliberately hidden from users — it's
                      internal noise. */}
                  {streamingPlanProgress && streamingPlanProgress.steps.length > 0 && (
                    <PlanExecutionProgress
                      title={streamingPlanProgress.title}
                      steps={streamingPlanProgress.steps}
                      isStreaming
                    />
                  )}
                  {streamingText ? (
                    <MarkdownMessage content={stripFileMarkers(streamingText)} variant="compact" />
                  ) : (
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      <Loader2 size={14} className="animate-spin" />
                      <span>{routingInfo ? 'Working...' : 'Thinking...'}</span>
                    </div>
                  )}
                  {/* Live status: short one-liner like "Searching tender
                      documents… 2.4s", driven by agent_status SSE events
                      from the callback handler. Cleared when tokens start
                      streaming or the run finishes. */}
                  {agentStatus && (
                    <AgentStatusPill
                      message={agentStatus.message}
                      startedAtMs={agentStatus.startedAtMs}
                    />
                  )}
                  {/* Reliability indicator: surfaced when the backend is
                      auto-recovering from truncation or empty content.
                      Without this users see a frozen UI and don't know
                      whether the agent is still working. */}
                  {reliabilityStatus && (
                    <div className={
                      reliabilityStatus.kind === 'prerequisite_running' || reliabilityStatus.kind === 'analyzer_progress' || reliabilityStatus.kind === 'costing_batch_progress' || reliabilityStatus.kind === 'costing_boq_extract'
                        ? 'mt-2 flex items-center gap-2 text-xs text-accent bg-accent/10 border border-accent/20 rounded px-2 py-1 w-fit'
                        : reliabilityStatus.kind === 'prerequisite_failed'
                          ? 'mt-2 flex items-center gap-2 text-xs text-destructive bg-destructive/10 border border-destructive/30 rounded px-2 py-1 w-fit'
                          : 'mt-2 flex items-center gap-2 text-xs text-warning bg-warning/10 border border-warning/20 rounded px-2 py-1 w-fit'
                    }>
                      {reliabilityStatus.kind === 'resumed' || reliabilityStatus.kind === 'prerequisite_complete' ? (
                        <>
                          <span className="text-success">✓</span>
                          <span>
                            {reliabilityStatus.kind === 'prerequisite_complete'
                              ? `Analysis ready${reliabilityStatus.detail ? ` — ${reliabilityStatus.detail}` : ''}.`
                              : `Resumed${reliabilityStatus.detail ? ` ${reliabilityStatus.detail}` : ''}.`}
                          </span>
                        </>
                      ) : reliabilityStatus.kind === 'prerequisite_failed' ? (
                        <>
                          <span>⚠</span>
                          <span>
                            Could not analyze the tender first
                            {reliabilityStatus.detail ? ` (${reliabilityStatus.detail})` : ''} — costing will proceed with raw scope only.
                          </span>
                        </>
                      ) : (
                        <>
                          <Loader2 size={12} className="animate-spin" />
                          <span>
                            {reliabilityStatus.kind === 'continuing' &&
                              `Continuing response${reliabilityStatus.attempt ? ` (${reliabilityStatus.attempt}/${reliabilityStatus.maxAttempts ?? 3})` : ''}…`}
                            {reliabilityStatus.kind === 'retried_empty' &&
                              'Retrying empty response…'}
                            {reliabilityStatus.kind === 'truncated' &&
                              'Response was cut short — recovering…'}
                            {reliabilityStatus.kind === 'empty' &&
                              'Got empty response — recovering…'}
                            {reliabilityStatus.kind === 'retried_no_thinking' &&
                              'Retrying without extended thinking…'}
                            {reliabilityStatus.kind === 'prerequisite_running' &&
                              `Analyzing the tender first to ensure accurate costing… (${reliabilityStatus.detail || 'Tender Document Analyzer'})`}
                            {reliabilityStatus.kind === 'analyzer_progress' && (
                              reliabilityStatus.phase === 'starting'
                                ? `Starting analysis of ${reliabilityStatus.total ?? 0} document${(reliabilityStatus.total ?? 0) === 1 ? '' : 's'}…`
                                : reliabilityStatus.phase === 'per_doc_complete'
                                  ? `Reading ${reliabilityStatus.completed} of ${reliabilityStatus.total} documents…`
                                  : reliabilityStatus.phase === 'synthesis_start'
                                    ? `Synthesising report from ${reliabilityStatus.total} document${reliabilityStatus.total === 1 ? '' : 's'}…`
                                    : reliabilityStatus.phase === 'complete'
                                      ? 'Analysis complete — rendering…'
                                      : 'Analysing…'
                            )}
                            {reliabilityStatus.kind === 'costing_batch_progress' && (
                              (reliabilityStatus.batch ?? 0) === 0
                                ? `Preparing to cost ${reliabilityStatus.linesTotal ?? 0} line item${(reliabilityStatus.linesTotal ?? 0) === 1 ? '' : 's'} in batches…`
                                : `Costing batch ${reliabilityStatus.batch} of ${reliabilityStatus.totalBatches} — ${reliabilityStatus.linesDone ?? 0}/${reliabilityStatus.linesTotal ?? 0} line items…`
                            )}
                            {reliabilityStatus.kind === 'costing_boq_extract' &&
                              'Extracting the NIT bidding schedule…'}
                          </span>
                        </>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
            </div>{/* end space-y-4 message list */}

            </div>{/* end min-h-full flex flex-col */}
          </div>{/* end scroll container */}

          {/* Scroll to bottom */}
          {showScrollBtn && (
            <div className="absolute bottom-32 left-1/2 -translate-x-1/2 z-10">
              <button
                onClick={() => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })}
                aria-label="Scroll to latest message"
                className="p-2 rounded-full bg-card border border-border shadow-lg hover:bg-muted text-foreground"
              >
                <ArrowDown size={16} />
              </button>
            </div>
          )}

          {/* Error */}
          {error && activeTab === 'chat' && (
            <div className="mx-6 mb-2 px-4 py-2 bg-destructive/10 border border-destructive/30 rounded-lg text-sm text-destructive">
              {error}
            </div>
          )}

          {/* Suggestions */}
          {suggestions.length > 0 && !streaming && canChat && (
            <div className="px-6 pb-2">
              <SuggestionBar
                suggestions={suggestions}
                onSelect={handleSuggestionSelect}
                disabled={streaming}
              />
            </div>
          )}

          {/* Input */}
          {canChat && (
            <div className="bg-gradient-to-t from-card via-card to-card/80 px-4 pb-4 pt-3 sm:px-6">
              {/* Pending file previews */}
              {pendingFiles.length > 0 && (
                <div className="mx-auto grid max-w-3xl gap-2 pb-2 sm:grid-cols-2">
                  {pendingFiles.map((f, i) => (
                    <div
                      key={i}
                      className="group flex min-w-0 items-center gap-3 rounded-xl border border-border bg-background p-2.5 text-xs shadow-sm"
                    >
                      <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[10px] font-bold uppercase ${f.name.toLowerCase().endsWith('.pdf') ? 'bg-red-500/10 text-red-600' : 'bg-sky-500/10 text-sky-700'}`}>
                        {f.name.split('.').pop()?.slice(0, 4) || 'FILE'}
                      </span>
                      <span className="min-w-0 flex-1"><span className="block truncate font-medium text-foreground">{f.name}</span><span className="mt-0.5 block text-[10px] text-muted-foreground">{formatFileSize(f.size)} · Ready to attach</span></span>
                      <button
                        onClick={() => removeFile(i)}
                        aria-label={`Remove ${f.name}`}
                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      >
                        <X size={13} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="mx-auto flex max-w-3xl items-end gap-1.5 rounded-2xl border border-border bg-background p-2 shadow-[0_8px_30px_rgba(15,23,42,0.08)] transition-shadow focus-within:border-emerald-500/40 focus-within:shadow-[0_10px_34px_rgba(5,150,105,0.10)]">
                <label
                  className="flex h-10 w-10 flex-shrink-0 cursor-pointer items-center justify-center rounded-xl text-muted-foreground hover:bg-muted hover:text-foreground"
                  title="Attach file"
                  aria-label="Attach file"
                >
                  <Paperclip size={18} />
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept=".pdf,.png,.jpg,.jpeg,.gif,.doc,.docx,.xls,.xlsx,.csv,.txt"
                    className="hidden"
                    onChange={handleFileSelect}
                  />
                </label>

                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={handleInputChange}
                  onKeyDown={handleKeyDown}
                  onPaste={handlePaste}
                  placeholder="Ask anything about tenders, proposals, or costing..."
                  rows={1}
                  aria-label="Message"
                  className="min-h-10 flex-1 resize-none overflow-y-auto bg-transparent px-2 py-2.5 text-sm leading-5 text-foreground outline-none placeholder:text-muted-foreground"
                  style={{ maxHeight: '120px' }}
                  disabled={streaming || uploading}
                />
                {streaming ? (
                  <button
                    onClick={stopGeneration}
                    disabled={stopping}
                    aria-label="Stop generating"
                    title={stopping ? 'Stopping the run…' : 'Stop generating'}
                    aria-busy={stopping}
                    className="flex h-10 w-10 items-center justify-center rounded-xl bg-destructive text-destructive-foreground hover:bg-destructive/90 disabled:opacity-60"
                  >
                    {stopping ? <Loader2 size={18} className="animate-spin" /> : <Square size={18} />}
                  </button>
                ) : (
                  <button
                    onClick={() => sendMessage()}
                    disabled={(!input.trim() && pendingFiles.length === 0) || uploading}
                    aria-label="Send message"
                    className="flex h-10 w-10 items-center justify-center rounded-xl bg-emerald-600 text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-35"
                  >
                    {uploading ? <Loader2 size={18} className="animate-spin" /> : <Send size={18} />}
                  </button>
                )}
              </div>
              <p className="mx-auto mt-2 max-w-3xl text-center text-[10px] text-muted-foreground">DRPL can make mistakes. Review important tender and costing details.</p>
            </div>
          )}
        </div>

        {/* ── Side-by-side artifact drawer (Claude-style). Sits beside the
             chat on desktop; overlays full-width on mobile. ── */}
        {artifactPanelOpen && (
          <div
            className={`flex min-h-0 ${
              artifactFullScreen
                ? 'w-full'
                : 'w-full md:flex-shrink-0 md:w-[52%] xl:w-[55%]'
            }`}
          >
            <ArtifactPanel
              artifacts={artifacts}
              selectedId={selectedArtifactId}
              onSelect={setSelectedArtifactId}
              onEdit={handleArtifactEdit}
              onExport={(id) => {
                const a = artifacts.find((art) => art.id === id);
                if (a) {
                  const blob = new Blob([a.content], { type: 'text/plain' });
                  const url = URL.createObjectURL(blob);
                  const link = document.createElement('a');
                  link.href = url;
                  link.download = `${a.title.replace(/[^a-z0-9]/gi, '_')}.txt`;
                  link.click();
                  URL.revokeObjectURL(url);
                }
              }}
              onClose={() => { userClosedPanel.current = true; setArtifactPanelOpen(false); setArtifactFullScreen(false); }}
              isOpen
              fullScreen={artifactFullScreen}
              onToggleFullScreen={() => setArtifactFullScreen((v) => !v)}
              tenderId={session?.tender_id}
              sessionId={session?.id}
              onArtifactsChanged={() => {
                if (session) {
                  getSessionArtifacts(session.id).then(setArtifacts);
                }
              }}
            />
          </div>
        )}
      </div>

      </div>{/* end horizontal split */}

      {/* Delete-chat confirmation (triggered from the sidebar) */}
      {deleteTargetId != null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4">
          <div className="w-full max-w-sm bg-card border border-border rounded-xl p-6 shadow-xl">
            <h3 className="text-lg font-semibold text-foreground mb-2">Delete chat</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Delete &quot;{sessionList.find((s) => s.id === deleteTargetId)?.tender_title
                || sessionList.find((s) => s.id === deleteTargetId)?.title
                || 'Untitled'}&quot;? This permanently removes its messages, results, and files. This cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setDeleteTargetId(null)}
                className="px-4 py-2 text-sm border border-border rounded-lg hover:bg-muted"
              >
                Cancel
              </button>
              <button
                onClick={handleSingleDelete}
                className="px-4 py-2 text-sm bg-destructive text-destructive-foreground rounded-lg hover:bg-destructive/90"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Clarification modal (Phase B) */}
      <ClarificationModal
        open={activeClarificationId != null}
        clarification={
          clarifications.find((c) => c.id === activeClarificationId) || null
        }
        onCancel={handleClarificationCancel}
        onSubmit={handleClarificationSubmit}
        submitting={clarificationSubmitting}
      />

      {/* Clarification toast — shows when modal is dismissed but answer still pending */}
      {activeClarificationId == null && clarifications.some((c) => c.status === 'pending') && (
        <button
          onClick={() => {
            const pending = clarifications.find((c) => c.status === 'pending');
            if (pending) setActiveClarificationId(pending.id);
          }}
          className="fixed bottom-5 right-5 z-40 flex items-center gap-2 rounded-full bg-amber-500 px-4 py-2.5 text-sm font-medium text-white shadow-lg transition hover:bg-amber-600"
        >
          <HelpCircle size={16} />
          {clarifications.filter((c) => c.status === 'pending').length} pending question
          {clarifications.filter((c) => c.status === 'pending').length === 1 ? '' : 's'}
        </button>
      )}

      <SessionSwitcherPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        sessions={sessionList}
        currentSessionId={session?.id ?? null}
        onSwitch={(id) => navigate(`/command-center/${id}`)}
        onNew={() => {
          setPaletteOpen(false);
          handleNewSession();
        }}
      />
    </div>
  );
}

function AttachmentPreview({ attachment }: { attachment: ChatAttachment }) {
  const extension = attachment.file_name.split('.').pop()?.toUpperCase() || 'FILE';
  const isPdf = extension === 'PDF' || attachment.file_type?.toLowerCase().includes('pdf');
  const size = attachment.file_size > 0
    ? attachment.file_size >= 1024 * 1024
      ? `${(attachment.file_size / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(attachment.file_size / 1024))} KB`
    : null;
  return (
    <div className="flex min-w-0 items-center gap-2.5 rounded-xl border border-border/80 bg-card/80 p-2.5 shadow-sm">
      <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[9px] font-extrabold tracking-wide ${isPdf ? 'bg-red-500/10 text-red-600 dark:text-red-400' : 'bg-sky-500/10 text-sky-700 dark:text-sky-400'}`}>
        {extension.slice(0, 4)}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium text-foreground" title={attachment.file_name}>{attachment.file_name}</span>
        <span className="mt-0.5 block text-[10px] text-muted-foreground">{size ? `${size} · ` : ''}Attached document</span>
      </span>
    </div>
  );
}
