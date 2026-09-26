import { useState, useEffect, useRef } from 'react';
import { Bot, ChevronDown, Check } from 'lucide-react';
import { getAvailableAgents } from '../../lib/api';

interface AgentOption {
  agent_key: string;
  display_name: string;
  description: string | null;
  document_categories: string[];
}

interface Props {
  tenderId: number;
  currentAgentKey: string | null;
  onSelect: (agentKey: string | null) => void;
}

export default function AgentAssignmentDropdown({ tenderId, currentAgentKey, onSelect }: Props) {
  const [agents, setAgents] = useState<AgentOption[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const data = await getAvailableAgents(tenderId);
        setAgents(data);
      } catch {
        // Ignore
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [tenderId]);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const currentAgent = agents.find((a) => a.agent_key === currentAgentKey);

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-sm px-3 py-1.5 border rounded-lg hover:bg-muted/40 transition-colors"
      >
        <Bot size={14} className="text-purple-600 dark:text-purple-400" />
        <span className="text-foreground">
          {currentAgent ? currentAgent.display_name : 'Choose DRPL helper'}
        </span>
        <ChevronDown size={14} className="text-muted-foreground" />
      </button>

      {open && (
        <div className="absolute top-full left-0 mt-1 w-72 bg-card border rounded-xl shadow-lg z-50 py-1 max-h-64 overflow-y-auto">
          {/* Unassign option */}
          <button
            onClick={() => { onSelect(null); setOpen(false); }}
            className={`w-full text-left px-3 py-2 text-sm hover:bg-muted/40 transition-colors flex items-center gap-2 ${
              !currentAgentKey ? 'bg-muted/40' : ''
            }`}
          >
            {!currentAgentKey && <Check size={14} className="text-indigo-600 dark:text-indigo-400" />}
            <span className={!currentAgentKey ? 'font-medium' : 'text-muted-foreground ml-5'}>
              Use the default helper
            </span>
          </button>

          <div className="border-t my-1" />

          {loading && (
            <div className="px-3 py-2 text-xs text-muted-foreground">Loading helpers...</div>
          )}

          {agents.map((agent) => (
            <button
              key={agent.agent_key}
              onClick={() => { onSelect(agent.agent_key); setOpen(false); }}
              className={`w-full text-left px-3 py-2 text-sm hover:bg-muted/40 transition-colors ${
                agent.agent_key === currentAgentKey ? 'bg-indigo-50 dark:bg-indigo-500/15' : ''
              }`}
            >
              <div className="flex items-center gap-2">
                {agent.agent_key === currentAgentKey && (
                  <Check size={14} className="text-indigo-600 dark:text-indigo-400 flex-shrink-0" />
                )}
                <div className={agent.agent_key === currentAgentKey ? '' : 'ml-5'}>
                  <div className="font-medium text-foreground">{agent.display_name}</div>
                  {agent.description && (
                    <div className="text-xs text-muted-foreground line-clamp-1 mt-0.5">
                      {agent.description}
                    </div>
                  )}
                  {agent.document_categories.length > 0 && (
                    <div className="flex gap-1 mt-1">
                      {agent.document_categories.map((cat) => (
                        <span
                          key={cat}
                          className="text-[10px] px-1.5 py-0.5 bg-purple-50 dark:bg-purple-500/15 text-purple-600 dark:text-purple-400 rounded"
                        >
                          {cat}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </button>
          ))}

          {!loading && agents.length === 0 && (
            <div className="px-3 py-2 text-xs text-muted-foreground">
              No specialist helpers are available. The default DRPL helper can still assist.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
