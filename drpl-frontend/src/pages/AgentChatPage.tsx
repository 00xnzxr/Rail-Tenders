import { useState, useRef, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Send, Bot, User, Loader2, Wrench } from 'lucide-react';
import { chatWithAgent, getAgentChatHistory, searchAgentMemories } from '../lib/api';
import ToolCallTrace from '../components/agents/ToolCallTrace';
import MemoryPanel from '../components/agents/MemoryPanel';

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  tool_calls?: any[];
  created_at?: string;
}

export default function AgentChatPage() {
  const { agentKey } = useParams<{ agentKey: string; sessionId?: string }>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string>('');
  const [memories, setMemories] = useState<any[]>([]);
  const [lastToolCalls, setLastToolCalls] = useState<any[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim() || !agentKey || loading) return;

    const userMessage = input.trim();
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setLoading(true);

    try {
      const result = await chatWithAgent(agentKey, userMessage, sessionId || undefined);

      if (result.session_id && !sessionId) {
        setSessionId(result.session_id);
      }

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: result.output || 'No response generated.',
        tool_calls: result.tool_calls,
      }]);

      setLastToolCalls(result.tool_calls || []);

      // Refresh memories
      try {
        const mems = await searchAgentMemories(userMessage, agentKey);
        setMemories(mems);
      } catch { /* ignore */ }

    } catch (err: any) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `Error: ${err.response?.data?.detail || err.message}`,
      }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex gap-4 h-[calc(100vh-7rem)]">
      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col bg-card rounded-xl border border-border shadow-sm">
        {/* Header */}
        <div className="px-5 py-3 border-b border-border flex items-center gap-3">
          <Bot size={20} className="text-accent" />
          <div>
            <h2 className="font-semibold text-foreground">{agentKey || 'Agent Chat'}</h2>
            <p className="text-xs text-muted-foreground">LangChain agent with tool calling</p>
          </div>
          {sessionId && (
            <span className="ml-auto text-xs text-muted-foreground font-mono">
              Session: {sessionId.substring(0, 8)}...
            </span>
          )}
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 && (
            <div className="text-center py-16 text-muted-foreground">
              <Bot size={48} className="mx-auto mb-3 opacity-40" />
              <p className="text-lg font-medium">Start a conversation</p>
              <p className="text-sm mt-1">This agent can use tools to search, analyze, and generate documents.</p>
            </div>
          )}

          {messages.map((msg, i) => (
            <div key={i} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
              {msg.role === 'assistant' && (
                <div className="w-8 h-8 rounded-full bg-accent/15 flex items-center justify-center flex-shrink-0">
                  <Bot size={16} className="text-accent" />
                </div>
              )}
              <div className={`max-w-[75%] space-y-2 ${msg.role === 'user' ? 'order-first' : ''}`}>
                <div className={`rounded-xl px-4 py-3 text-sm leading-relaxed ${
                  msg.role === 'user'
                    ? 'bg-accent text-accent-foreground'
                    : 'bg-muted/40 text-foreground border border-border'
                }`}>
                  <pre className="whitespace-pre-wrap font-sans">{msg.content}</pre>
                </div>

                {/* Tool calls */}
                {msg.tool_calls && msg.tool_calls.length > 0 && (
                  <div className="space-y-1">
                    <p className="text-xs text-muted-foreground flex items-center gap-1">
                      <Wrench size={12} /> {msg.tool_calls.length} tool call(s)
                    </p>
                    {msg.tool_calls.map((tc: any, j: number) => (
                      <ToolCallTrace key={j} tool={tc.tool} input={tc.input} />
                    ))}
                  </div>
                )}
              </div>
              {msg.role === 'user' && (
                <div className="w-8 h-8 rounded-full bg-muted flex items-center justify-center flex-shrink-0">
                  <User size={16} className="text-muted-foreground" />
                </div>
              )}
            </div>
          ))}

          {loading && (
            <div className="flex gap-3">
              <div className="w-8 h-8 rounded-full bg-accent/15 flex items-center justify-center">
                <Loader2 size={16} className="text-accent animate-spin" />
              </div>
              <div className="bg-muted/40 rounded-xl px-4 py-3 border border-border">
                <p className="text-sm text-muted-foreground">Thinking and using tools...</p>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="p-4 border-t border-border">
          <div className="flex gap-2">
            <input
              type="text"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && !e.shiftKey && sendMessage()}
              placeholder="Type a message..."
              className="flex-1 px-4 py-2.5 border border-border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
              disabled={loading}
            />
            <button
              onClick={sendMessage}
              disabled={!input.trim() || loading}
              className="px-4 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <Send size={16} />
            </button>
          </div>
        </div>
      </div>

      {/* Memory Sidebar */}
      <div className="w-80 bg-card rounded-xl border border-border shadow-sm p-4 overflow-y-auto">
        <MemoryPanel memories={memories} title="Related Memories" />
      </div>
    </div>
  );
}
