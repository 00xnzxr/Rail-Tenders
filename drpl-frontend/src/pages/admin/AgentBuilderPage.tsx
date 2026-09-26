import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Cpu, Plus, Copy, Trash2, Settings, Zap, Search, Filter,
  Brain, RefreshCw, Wrench, GitBranch, MessageSquare,
  ChevronRight, BarChart3, Activity, FileText,
} from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getBuilderAgents, deleteBuilderAgent, cloneBuilderAgent, createBuilderAgent } from '../../lib/api';

const AGENT_TYPE_COLORS: Record<string, string> = {
  chain_of_thought: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  react: 'bg-accent/15 text-accent',
  tool_use: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  orchestrator: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
};

const AGENT_TYPE_ICONS: Record<string, any> = {
  chain_of_thought: Brain,
  react: RefreshCw,
  tool_use: Wrench,
  orchestrator: GitBranch,
};

const CATEGORY_OPTIONS = ['All', 'general', 'analysis', 'generation', 'extraction', 'orchestration'];
const TYPE_OPTIONS = ['All', 'chain_of_thought', 'react', 'tool_use', 'orchestrator'];

const AGENT_TEMPLATES = [
  {
    name: 'Tender Analyzer',
    description: 'Analyzes tender documents in-depth: extracts requirements, detects negative keywords, identifies rejection risks.',
    agent_type: 'react', category: 'analysis',
    system_prompt: 'You are a tender document analysis expert. Analyze uploaded tender documents thoroughly. Extract all requirements categorized by: technical, financial, eligibility, compliance, documentation, timeline, and special conditions. Detect negative keywords and rejection-triggering sentences.',
    tags: ['tender', 'analysis', 'nlp'],
    tools: [],
  },
  {
    name: 'Proposal Writer',
    description: 'Generates proposal documents per checklist using tender context and web research.',
    agent_type: 'react', category: 'generation',
    system_prompt: 'You are a professional tender proposal writer for DRPL (an Indian Railways contractor). Generate high-quality proposal documents that address the specific tender requirements. Each document should be formatted professionally.',
    tags: ['proposal', 'generation', 'documents'],
    tools: [],
  },
  {
    name: 'Costing Agent',
    description: 'Researches market rates and generates detailed cost breakdowns with GST calculations.',
    agent_type: 'react', category: 'analysis',
    system_prompt: 'You are a costing expert for Indian Railways tenders. Research current market rates and produce itemized cost breakdowns. Include base rates, quantities, overhead percentages, GST calculations, and a grand total.',
    tags: ['costing', 'calculation', 'research'],
    tools: [],
  },
  {
    name: 'Document Processor',
    description: 'Simple agent that processes and summarizes documents without tool calling.',
    agent_type: 'chain_of_thought', category: 'general',
    system_prompt: 'You are a document processing assistant. Summarize, extract key points, and organize the information from the provided document text in a clear, structured format.',
    tags: ['documents', 'summary'],
    tools: [],
  },
];

export default function AgentBuilderPage() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('All');
  const [typeFilter, setTypeFilter] = useState('All');
  const [showTemplates, setShowTemplates] = useState(false);

  const load = () => {
    setLoading(true);
    const params: Record<string, any> = {};
    if (categoryFilter !== 'All') params.category = categoryFilter;
    getBuilderAgents(params)
      .then(setAgents)
      .catch(() => setAgents([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [categoryFilter]);

  const filtered = agents.filter(a => {
    if (typeFilter !== 'All' && a.agent_type !== typeFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (a.display_name || '').toLowerCase().includes(q) ||
        (a.description || '').toLowerCase().includes(q) ||
        (a.agent_key || '').toLowerCase().includes(q);
    }
    return true;
  });

  // Hierarchy, not provenance. The Master Agent governs the platform and calls
  // every worker below it, so it is presented on its own rather than mixed in
  // with the agents it directs. `agent_role` comes from the backend; the
  // agent_key fallback keeps this correct against a cached response from
  // before that field existed.
  const masterAgent = filtered.find(
    a => a.agent_role === 'master' || a.agent_key === 'decision_maker',
  );
  const workerAgents = filtered.filter(
    a => a !== masterAgent && a.agent_role !== 'infrastructure' && a.agent_key !== 'proposal_router',
  );
  const infrastructureAgents = filtered.filter(
    a => a !== masterAgent && (a.agent_role === 'infrastructure' || a.agent_key === 'proposal_router'),
  );
  const systemWorkers = workerAgents.filter(a => a.is_system);
  const customWorkers = workerAgents.filter(a => !a.is_system);

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`Delete agent "${name}"? This cannot be undone.`)) return;
    try { await deleteBuilderAgent(id); load(); } catch { /* ignore */ }
  };

  const handleClone = async (id: number, name: string) => {
    const newName = prompt('Name for the cloned agent:', `${name} (Copy)`);
    if (!newName) return;
    try {
      const cloned = await cloneBuilderAgent(id, newName);
      navigate(`/admin/agent-builder/${cloned.id}`);
    } catch { /* ignore */ }
  };

  const handleCreateFromTemplate = async (template: typeof AGENT_TEMPLATES[0]) => {
    try {
      const body = {
        display_name: template.name,
        agent_key: template.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, ''),
        description: template.description,
        agent_type: template.agent_type,
        category: template.category,
        system_prompt: template.system_prompt,
        tags: template.tags,
        tools: template.tools,
        temperature: template.agent_type === 'react' ? 0.3 : 0.7,
        max_tokens: template.agent_type === 'react' ? 8192 : 4096,
      };
      const created = await createBuilderAgent(body);
      setShowTemplates(false);
      navigate(`/admin/agent-builder/${created.id}`);
    } catch { /* ignore */ }
  };

  if (loading) return <><Header title="Agent Builder" /><LoadingSpinner /></>;

  const AgentCard = ({ agent }: { agent: any }) => {
    const TypeIcon = AGENT_TYPE_ICONS[agent.agent_type] || Cpu;
    return (
      <div
        className="bg-card rounded-xl shadow-sm border border-border p-5 hover:shadow-md transition-shadow cursor-pointer group"
        onClick={() => navigate(`/admin/agent-builder/${agent.id}`)}
      >
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2.5">
            <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${
              agent.is_enabled ? 'bg-accent/10' : 'bg-muted'
            }`}>
              <TypeIcon size={18} className={agent.is_enabled ? 'text-accent' : 'text-muted-foreground'} />
            </div>
            <div>
              <h3 className="font-semibold text-foreground text-sm">{agent.display_name}</h3>
              <p className="text-[10px] text-muted-foreground font-mono">{agent.agent_key}</p>
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            {agent.is_system && (
              <span className="text-[9px] px-1.5 py-0.5 bg-foreground/80 text-background rounded font-medium">SYSTEM</span>
            )}
            <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${AGENT_TYPE_COLORS[agent.agent_type] || 'bg-muted text-muted-foreground'}`}>
              {agent.agent_type}
            </span>
          </div>
        </div>

        <p className="text-xs text-muted-foreground mb-3 line-clamp-2 leading-relaxed">{agent.description || 'No description'}</p>

        <div className="flex flex-wrap gap-1.5 mb-3">
          {agent.category && (
            <span className="text-[10px] px-2 py-0.5 bg-accent/10 text-accent rounded-full">{agent.category}</span>
          )}
          {(agent.tags || []).slice(0, 3).map((tag: string) => (
            <span key={tag} className="text-[10px] px-2 py-0.5 bg-muted/40 text-muted-foreground rounded-full">{tag}</span>
          ))}
        </div>

        <div className="flex items-center justify-between text-[11px] text-muted-foreground pt-3 border-t border-border">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1"><Wrench size={11} /> {agent.tools_count || 0} tools</span>
            <span className="flex items-center gap-1"><Zap size={11} /> v{agent.current_version || 1}</span>
          </div>
          <div className={`w-2 h-2 rounded-full ${agent.is_enabled ? 'bg-emerald-400' : 'bg-muted-foreground/40'}`} title={agent.is_enabled ? 'Enabled' : 'Disabled'} />
        </div>

        {/* Quick actions */}
        <div
          className="flex items-center gap-2 mt-3 pt-3 border-t border-border opacity-0 group-hover:opacity-100 transition-opacity"
          onClick={e => e.stopPropagation()}
        >
          <button onClick={() => navigate(`/admin/agent-builder/${agent.id}`)}
            className="text-xs text-muted-foreground hover:text-accent flex items-center gap-1">
            <Settings size={12} /> Edit
          </button>
          <button onClick={() => handleClone(agent.id, agent.display_name)}
            className="text-xs text-muted-foreground hover:text-accent flex items-center gap-1">
            <Copy size={12} /> Clone
          </button>
          <button onClick={() => navigate(`/agent-chat/${agent.agent_key}`)}
            className="text-xs text-muted-foreground hover:text-accent flex items-center gap-1">
            <MessageSquare size={12} /> Chat
          </button>
          {!agent.is_system && (
            <button onClick={() => handleDelete(agent.id, agent.display_name)}
              className="text-xs text-red-400 hover:text-red-600 dark:text-red-400 flex items-center gap-1 ml-auto">
              <Trash2 size={12} /> Delete
            </button>
          )}
        </div>
      </div>
    );
  };

  return (
    <>
      <Header title="Agent Builder" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Top bar */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3 flex-1 w-full sm:w-auto">
            <div className="relative flex-1 max-w-md">
              <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input type="text" placeholder="Search agents..."
                value={search} onChange={e => setSearch(e.target.value)}
                className="w-full pl-9 pr-3 py-2 text-sm border border-border rounded-lg focus:ring-2 focus:ring-ring" />
            </div>
            <select value={categoryFilter} onChange={e => setCategoryFilter(e.target.value)}
              className="px-3 py-2 text-sm border border-border rounded-lg bg-card">
              {CATEGORY_OPTIONS.map(c => (
                <option key={c} value={c}>{c === 'All' ? 'All Categories' : c}</option>
              ))}
            </select>
            <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)}
              className="px-3 py-2 text-sm border border-border rounded-lg bg-card">
              {TYPE_OPTIONS.map(t => (
                <option key={t} value={t}>{t === 'All' ? 'All Types' : t}</option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setShowTemplates(true)}
              className="px-4 py-2 text-sm font-medium border border-border rounded-lg hover:bg-muted/40 flex items-center gap-2">
              <FileText size={16} /> From Template
            </button>
            <button onClick={() => navigate('/admin/agent-builder/new')}
              className="bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 flex items-center gap-2">
              <Plus size={16} /> Create Agent
            </button>
          </div>
        </div>

        {/* Agent grid */}
        {filtered.length === 0 ? (
          <div className="text-center py-20 text-muted-foreground">
            <Cpu size={48} className="mx-auto mb-3 opacity-40" />
            <p className="text-lg font-medium">No agents found</p>
            <p className="text-sm mt-1">Create your first agent or use a template to get started.</p>
          </div>
        ) : (
          <>
            {/* Master Agent — the governing agent, shown above its workers */}
            {masterAgent && (
              <div>
                <div className="flex items-center gap-2 mb-4">
                  <h2 className="text-sm font-semibold text-foreground">Master Agent</h2>
                  <span className="text-[10px] bg-foreground text-background px-2 py-0.5 rounded-full font-medium">
                    governs the platform
                  </span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  <AgentCard agent={masterAgent} />
                </div>
                <p className="text-xs text-muted-foreground mt-3">
                  Calls any enabled worker agent below. Disabling a worker here removes it
                  from what the Master Agent can call.
                </p>
              </div>
            )}

            {/* Worker Agents — everything the Master Agent can delegate to */}
            {workerAgents.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-4">
                  <h2 className="text-sm font-semibold text-muted-foreground">Worker Agents</h2>
                  <span className="text-[10px] bg-muted text-muted-foreground px-2 py-0.5 rounded-full">{workerAgents.length}</span>
                </div>

                {systemWorkers.length > 0 && (
                  <div className="mb-6">
                    <h3 className="text-xs font-medium text-muted-foreground mb-3">Built-in</h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                      {systemWorkers.map(agent => <AgentCard key={agent.id} agent={agent} />)}
                    </div>
                  </div>
                )}

                {customWorkers.length > 0 && (
                  <div>
                    <h3 className="text-xs font-medium text-muted-foreground mb-3">Custom</h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                      {customWorkers.map(agent => <AgentCard key={agent.id} agent={agent} />)}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Routing infrastructure — not callable as workers */}
            {infrastructureAgents.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-4">
                  <h2 className="text-sm font-semibold text-muted-foreground">Routing</h2>
                  <span className="text-[10px] bg-muted text-muted-foreground px-2 py-0.5 rounded-full">{infrastructureAgents.length}</span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {infrastructureAgents.map(agent => <AgentCard key={agent.id} agent={agent} />)}
                </div>
                <p className="text-xs text-muted-foreground mt-3">
                  Serves the routing layer. Not callable by the Master Agent.
                </p>
              </div>
            )}

            {systemWorkers.length > 0 && customWorkers.length === 0 && !search && (
              <div className="text-center py-8 text-muted-foreground">
                <p className="text-sm">No custom agents yet. Create one or use a template to get started.</p>
              </div>
            )}
          </>
        )}

        {/* Template Modal */}
        {showTemplates && (
          <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowTemplates(false)}>
            <div className="bg-card rounded-xl shadow-xl w-full max-w-2xl p-6 max-h-[85vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
              <h2 className="text-lg font-semibold text-foreground mb-1">Create from Template</h2>
              <p className="text-sm text-muted-foreground mb-5">Choose a pre-configured agent template to get started quickly.</p>
              <div className="space-y-3">
                {AGENT_TEMPLATES.map((template, i) => {
                  const TypeIcon = AGENT_TYPE_ICONS[template.agent_type] || Cpu;
                  return (
                    <button key={i} onClick={() => handleCreateFromTemplate(template)}
                      className="w-full text-left p-4 border border-border rounded-xl hover:border-accent/40 hover:bg-accent/10/30 transition-all">
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-lg bg-accent/10 flex items-center justify-center">
                          <TypeIcon size={20} className="text-accent" />
                        </div>
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <h3 className="font-semibold text-sm text-foreground">{template.name}</h3>
                            <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${AGENT_TYPE_COLORS[template.agent_type]}`}>
                              {template.agent_type}
                            </span>
                            <span className="text-[10px] px-2 py-0.5 bg-muted text-muted-foreground rounded-full">{template.category}</span>
                          </div>
                          <p className="text-xs text-muted-foreground mt-1">{template.description}</p>
                        </div>
                        <ChevronRight size={16} className="text-muted-foreground" />
                      </div>
                    </button>
                  );
                })}
              </div>
              <div className="flex justify-end mt-5">
                <button onClick={() => setShowTemplates(false)} className="px-4 py-2 text-sm text-muted-foreground hover:bg-muted rounded-lg">
                  Cancel
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
