import { useState, useEffect, useRef, useCallback } from 'react';
import { Bot, Send, Loader2, StopCircle, Sparkles, Wand2, FileDown, Check } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getDocumentAgentHistory } from '../../lib/api';
import { API_BASE } from '../../lib/api';

const GENERATION_KEYWORDS = /\b(generate|write|create|draft|produce|compose|prepare|build)\b/i;

interface ChatMessage {
  id?: number;
  role: 'user' | 'assistant';
  content: string;
  agent_key?: string;
  created_at?: string;
}

interface Props {
  tenderId: number;
  itemId: number;
  agentKey: string | null;
  agentName?: string;
  onContentGenerated?: (html: string) => void;
}

export default function DocumentAgentChatPanel({
  tenderId,
  itemId,
  agentKey,
  agentName,
  onContentGenerated,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [currentAgent, setCurrentAgent] = useState(agentName || agentKey || 'Document Agent');
  const [loading, setLoading] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Load conversation history
  const loadHistory = useCallback(async () => {
    try {
      const history = await getDocumentAgentHistory(tenderId, itemId);
      setMessages(
        history.map((h: any) => ({
          id: h.id,
          role: h.role,
          content: h.content,
          agent_key: h.agent_key,
          created_at: h.created_at,
        }))
      );
    } catch {
      // No history yet — that's fine
    } finally {
      setLoading(false);
    }
  }, [tenderId, itemId]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingText]);

  const [appliedMsgIds, setAppliedMsgIds] = useState<Set<number>>(new Set());
  const lastUserMsgRef = useRef('');

  const handleApplyToCanvas = (content: string, msgIndex: number) => {
    onContentGenerated?.(content);
    setAppliedMsgIds((prev) => new Set(prev).add(msgIndex));
  };

  const handleSend = async () => {
    const msg = input.trim();
    if (!msg || streaming) return;

    setInput('');
    lastUserMsgRef.current = msg;
    setMessages((prev) => [...prev, { role: 'user', content: msg }]);
    setStreaming(true);
    setStreamingText('');

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const token = localStorage.getItem('drpl_token');
      const response = await fetch(
        `${API_BASE}/api/tenders/${tenderId}/workspace/items/${itemId}/agent/chat`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({ message: msg }),
          signal: controller.signal,
        }
      );

      const reader = response.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let buffer = '';
      let fullText = '';
      let eventType = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));

              if (eventType === 'agent_start') {
                setCurrentAgent(data.agent_name || data.agent_key || 'Agent');
              } else if (eventType === 'token') {
                fullText += data.text;
                setStreamingText(fullText);
              } else if (eventType === 'agent_complete') {
                // Streaming complete
              } else if (eventType === 'error') {
                fullText += `\n\n**Error:** ${data.message}`;
                setStreamingText(fullText);
              }
            } catch {
              // Ignore parse errors
            }
          }
        }
      }

      // Finalize: add to messages
      if (fullText) {
        const newMsgIndex = messages.length + 1; // +1 because user msg was already added
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: fullText, agent_key: agentKey || undefined },
        ]);

        // Auto-apply to canvas if the user asked to generate/write content
        if (GENERATION_KEYWORDS.test(lastUserMsgRef.current)) {
          onContentGenerated?.(fullText);
          setAppliedMsgIds((prev) => new Set(prev).add(newMsgIndex));
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: `**Error:** ${err.message || 'Connection failed'}` },
        ]);
      }
    } finally {
      setStreaming(false);
      setStreamingText('');
      abortRef.current = null;
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col h-full bg-card border rounded-xl overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3 border-b bg-muted/40">
        <Bot size={16} className="text-purple-600 dark:text-purple-400" />
        <span className="text-sm font-semibold text-foreground">DRPL document helper</span>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4 min-h-0">
        {loading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 size={20} className="animate-spin text-muted-foreground" />
          </div>
        )}

        {!loading && messages.length === 0 && !streaming && (
          <div className="text-center py-8 text-muted-foreground">
            <Sparkles size={24} className="mx-auto mb-2" />
            <p className="text-sm">Ask DRPL to help with this document</p>
            <div className="mt-3 space-y-1">
              <button
                onClick={() => setInput('Generate the full content for this document following the tender requirements.')}
                className="block w-full text-xs text-left px-3 py-2 bg-muted/40 hover:bg-muted rounded-lg text-muted-foreground transition-colors"
              >
                <Wand2 size={12} className="inline mr-1" />
                Generate full document content
              </button>
              <button
                onClick={() => setInput('What format and sections should this document include based on the tender requirements?')}
                className="block w-full text-xs text-left px-3 py-2 bg-muted/40 hover:bg-muted rounded-lg text-muted-foreground transition-colors"
              >
                <Bot size={12} className="inline mr-1" />
                Ask about required format
              </button>
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={msg.id || i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[85%] rounded-xl px-3 py-2 text-sm ${
                msg.role === 'user'
                  ? 'bg-indigo-600 text-white'
                  : 'bg-muted text-foreground'
              }`}
            >
              {msg.role === 'assistant' ? (
                <>
                  <div className="prose prose-sm max-w-none prose-p:my-1 prose-li:my-0">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {msg.content}
                    </ReactMarkdown>
                  </div>
                  {onContentGenerated && msg.content.length > 50 && (
                    <button
                      onClick={() => handleApplyToCanvas(msg.content, i)}
                      disabled={appliedMsgIds.has(i)}
                      className={`mt-2 flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-md transition-colors ${
                        appliedMsgIds.has(i)
                          ? 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400 cursor-default'
                          : 'bg-indigo-50 dark:bg-indigo-500/15 text-indigo-700 dark:text-indigo-400 hover:bg-indigo-100 dark:bg-indigo-500/20'
                      }`}
                    >
                      {appliedMsgIds.has(i) ? (
                        <><Check size={11} /> Applied to canvas</>
                      ) : (
                        <><FileDown size={11} /> Apply to canvas</>
                      )}
                    </button>
                  )}
                </>
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
            </div>
          </div>
        ))}

        {/* Streaming response */}
        {streaming && streamingText && (
          <div className="flex justify-start">
            <div className="max-w-[85%] rounded-xl px-3 py-2 text-sm bg-muted text-foreground">
              <div className="prose prose-sm max-w-none prose-p:my-1 prose-li:my-0">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {streamingText}
                </ReactMarkdown>
              </div>
            </div>
          </div>
        )}

        {streaming && !streamingText && (
          <div className="flex justify-start">
            <div className="rounded-xl px-3 py-2 bg-muted">
              <Loader2 size={16} className="animate-spin text-muted-foreground" />
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t px-3 py-2">
        {streaming ? (
          <button
            onClick={handleStop}
            className="w-full flex items-center justify-center gap-2 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:bg-red-500/15 rounded-lg transition-colors"
          >
            <StopCircle size={14} />
            Stop generating
          </button>
        ) : (
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask DRPL about this document..."
              rows={1}
              className="flex-1 resize-none border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent max-h-24"
              style={{ minHeight: '38px' }}
            />
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className="flex-shrink-0 p-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Send size={16} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
