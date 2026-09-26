import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Library, Copy, Search, Tag, Cpu } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getAgentLibrary, cloneBuilderAgent } from '../../lib/api';

const AGENT_TYPE_COLORS: Record<string, string> = {
  chain_of_thought: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  react: 'bg-accent/15 text-accent',
  tool_use: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  orchestrator: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
};

const CATEGORY_OPTIONS = ['All', 'general', 'analysis', 'generation', 'extraction', 'orchestration'];

export default function AgentLibraryPage() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('All');

  useEffect(() => {
    setLoading(true);
    getAgentLibrary()
      .then(setAgents)
      .catch(() => setAgents([]))
      .finally(() => setLoading(false));
  }, []);

  const handleClone = async (id: number, name: string) => {
    const newName = prompt('Name for the cloned agent:', `${name} (My Copy)`);
    if (!newName) return;
    try {
      const cloned = await cloneBuilderAgent(id, newName);
      navigate(`/admin/agent-builder/${cloned.id}`);
    } catch { /* ignore */ }
  };

  const filtered = agents.filter((a) => {
    if (categoryFilter !== 'All' && a.category !== categoryFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        (a.display_name || '').toLowerCase().includes(q) ||
        (a.description || '').toLowerCase().includes(q) ||
        (a.tags || []).some((t: string) => t.toLowerCase().includes(q))
      );
    }
    return true;
  });

  if (loading) return <><Header title="Agent Library" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Agent Library" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Top bar */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
          <div className="relative flex-1 max-w-md">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search published agents..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-9 pr-3 py-2 text-sm border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-ring focus:border-ring"
            />
          </div>
          <div className="flex items-center gap-2">
            <Tag size={14} className="text-muted-foreground" />
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              className="px-3 py-2 text-sm border border-border rounded-lg appearance-none bg-card focus:outline-none focus:ring-2 focus:ring-ring focus:border-ring"
            >
              {CATEGORY_OPTIONS.map((c) => (
                <option key={c} value={c}>{c === 'All' ? 'All Categories' : c}</option>
              ))}
            </select>
          </div>
        </div>

        {/* Agent grid */}
        {filtered.length === 0 ? (
          <div className="text-center py-20 text-muted-foreground">
            <Library size={48} className="mx-auto mb-3 opacity-40" />
            <p className="text-lg font-medium">No published agents</p>
            <p className="text-sm mt-1">Published agent templates will appear here.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
            {filtered.map((agent) => (
              <div
                key={agent.id}
                className="bg-card rounded-xl shadow-sm border border-border p-5 hover:shadow-md transition-shadow"
              >
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <Cpu size={18} className="text-accent" />
                    <h3 className="font-semibold text-foreground text-sm">{agent.display_name}</h3>
                  </div>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${AGENT_TYPE_COLORS[agent.agent_type] || 'bg-muted text-muted-foreground'}`}>
                    {agent.agent_type}
                  </span>
                </div>

                <p className="text-xs text-muted-foreground mb-3 line-clamp-3">{agent.description || 'No description'}</p>

                <div className="flex flex-wrap gap-1.5 mb-4">
                  {agent.category && (
                    <span className="text-[10px] px-2 py-0.5 bg-accent/10 text-accent rounded-full">{agent.category}</span>
                  )}
                  {(agent.tags || []).slice(0, 4).map((tag: string) => (
                    <span key={tag} className="text-[10px] px-2 py-0.5 bg-muted/40 text-muted-foreground rounded-full">{tag}</span>
                  ))}
                </div>

                <button
                  onClick={() => handleClone(agent.id, agent.display_name)}
                  className="w-full bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 flex items-center justify-center gap-2"
                >
                  <Copy size={14} /> Clone to My Agents
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
