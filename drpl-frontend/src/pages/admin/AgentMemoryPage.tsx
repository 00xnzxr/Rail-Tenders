import { useState, useEffect } from 'react';
import {
  Brain, Search, Trash2, Plus, Tag, RefreshCw, BarChart3,
  Clock, Eye, Zap, Layers, BookOpen, Lightbulb, Scale, Star,
  Calendar, ChevronDown, Upload, FileText, X, CheckCircle2, Loader2,
} from 'lucide-react';
import {
  getAgentMemories, deleteAgentMemory, createAgentMemory, searchAgentMemories, getMemoryStats,
  parseMemoryMd, commitMemoryUpload,
} from '../../lib/api';

interface Memory {
  id: number;
  agent_key?: string;
  memory_type: string;
  content: string;
  context?: string;
  keywords: string[];
  importance: number;
  access_count: number;
  tender_id?: number;
  created_at: string;
  last_accessed_at?: string;
  expires_at?: string;
}

const TYPE_OPTIONS = ['fact', 'preference', 'learning', 'decision'];
const TYPE_COLORS: Record<string, string> = {
  fact: 'bg-accent/15 text-accent border-accent/20',
  preference: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400 border-purple-200 dark:border-purple-500/20',
  learning: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20',
  decision: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400 border-amber-200 dark:border-amber-500/20',
};
const TYPE_ICONS: Record<string, any> = {
  fact: BookOpen,
  preference: Star,
  learning: Lightbulb,
  decision: Scale,
};
const TYPE_BG: Record<string, string> = {
  fact: 'border-l-blue-400',
  preference: 'border-l-purple-400',
  learning: 'border-l-emerald-400',
  decision: 'border-l-amber-400',
};

export default function AgentMemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterType, setFilterType] = useState('');
  const [filterAgent, setFilterAgent] = useState('');
  const [showAddModal, setShowAddModal] = useState(false);
  const [viewMode, setViewMode] = useState<'grid' | 'timeline'>('grid');

  // Upload MD state
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadAgentKey, setUploadAgentKey] = useState('');
  const [useAiParsing, setUseAiParsing] = useState(false);
  const [parsedEntries, setParsedEntries] = useState<any[]>([]);
  const [uploadStep, setUploadStep] = useState<'select' | 'preview' | 'done'>('select');
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [createdCount, setCreatedCount] = useState(0);

  const [newMemory, setNewMemory] = useState({
    memory_type: 'fact',
    content: '',
    agent_key: '',
    keywords: '',
    importance: 0.5,
    context: '',
    tender_id: '',
  });

  const loadMemories = async () => {
    setLoading(true);
    try {
      const params: Record<string, any> = {};
      if (filterType) params.memory_type = filterType;
      if (filterAgent) params.agent_key = filterAgent;
      const data = await getAgentMemories(params);
      setMemories(data);
    } catch { /* ignore */ }
    setLoading(false);
  };

  const loadStats = async () => {
    try {
      const data = await getMemoryStats();
      setStats(data);
    } catch { /* ignore */ }
  };

  useEffect(() => { loadMemories(); loadStats(); }, [filterType, filterAgent]);

  const handleSearch = async () => {
    if (!searchQuery.trim()) { loadMemories(); return; }
    setLoading(true);
    try {
      const data = await searchAgentMemories(searchQuery, filterAgent || undefined);
      setMemories(data);
    } catch { /* ignore */ }
    setLoading(false);
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this memory?')) return;
    try {
      await deleteAgentMemory(id);
      setMemories(prev => prev.filter(m => m.id !== id));
      loadStats();
    } catch { /* ignore */ }
  };

  const handleAdd = async () => {
    try {
      await createAgentMemory({
        memory_type: newMemory.memory_type,
        content: newMemory.content,
        agent_key: newMemory.agent_key || undefined,
        keywords: newMemory.keywords ? newMemory.keywords.split(',').map(k => k.trim()) : [],
        importance: newMemory.importance,
        context: newMemory.context || undefined,
        tender_id: newMemory.tender_id ? parseInt(newMemory.tender_id) : undefined,
      });
      setShowAddModal(false);
      setNewMemory({ memory_type: 'fact', content: '', agent_key: '', keywords: '', importance: 0.5, context: '', tender_id: '' });
      loadMemories();
      loadStats();
    } catch { /* ignore */ }
  };

  const handleParseMd = async () => {
    if (!uploadFile) return;
    setUploading(true);
    setUploadError('');
    try {
      const result = await parseMemoryMd(uploadFile, uploadAgentKey || undefined, useAiParsing);
      setParsedEntries(result.entries);
      setUploadStep('preview');
    } catch (err: any) {
      setUploadError(err?.response?.data?.detail || 'Failed to parse file');
    }
    setUploading(false);
  };

  const handleCommitUpload = async () => {
    setUploading(true);
    setUploadError('');
    try {
      const result = await commitMemoryUpload(parsedEntries, uploadAgentKey || undefined);
      setCreatedCount(result.count);
      setUploadStep('done');
      loadMemories();
      loadStats();
    } catch (err: any) {
      setUploadError(err?.response?.data?.detail || 'Failed to save memories');
    }
    setUploading(false);
  };

  const resetUploadModal = () => {
    setShowUploadModal(false);
    setUploadFile(null);
    setUploadAgentKey('');
    setUseAiParsing(false);
    setParsedEntries([]);
    setUploadStep('select');
    setUploadError('');
    setCreatedCount(0);
  };

  const uniqueAgents = [...new Set(memories.map(m => m.agent_key).filter(Boolean))] as string[];

  // Group memories by date for timeline view
  const groupedByDate: Record<string, Memory[]> = {};
  memories.forEach(m => {
    const date = new Date(m.created_at).toLocaleDateString();
    if (!groupedByDate[date]) groupedByDate[date] = [];
    groupedByDate[date].push(m);
  });

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Agent Memory Store</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Long-term memory for LangChain agents — facts, preferences, learnings, and decisions
          </p>
        </div>
        <button
          onClick={() => setShowAddModal(true)}
          className="flex items-center gap-2 px-4 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 text-sm font-medium transition-colors shadow-sm"
        >
          <Plus size={16} /> Add Memory
        </button>
        <button
          onClick={() => setShowUploadModal(true)}
          className="flex items-center gap-2 px-4 py-2.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm font-medium transition-colors shadow-sm"
        >
          <Upload size={16} /> Upload Knowledge File
        </button>
      </div>

      {/* Stats Dashboard */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <div className="bg-card rounded-xl border border-border shadow-sm p-4">
          <div className="flex items-center gap-2 mb-2">
            <div className="p-1.5 bg-muted rounded-lg"><Brain size={14} className="text-muted-foreground" /></div>
            <span className="text-xs text-muted-foreground font-semibold uppercase tracking-wide">Total</span>
          </div>
          <p className="text-2xl font-bold text-foreground">{stats?.total || 0}</p>
        </div>
        {TYPE_OPTIONS.map(type => {
          const Icon = TYPE_ICONS[type] || Brain;
          const count = stats?.by_type?.[type] || 0;
          return (
            <div key={type} className="bg-card rounded-xl border border-border shadow-sm p-4">
              <div className="flex items-center gap-2 mb-2">
                <div className={`p-1.5 rounded-lg ${TYPE_COLORS[type]}`}><Icon size={14} /></div>
                <span className="text-xs text-muted-foreground font-semibold uppercase tracking-wide">{type}</span>
              </div>
              <p className="text-2xl font-bold text-foreground">{count}</p>
              {stats?.total > 0 && (
                <div className="mt-2 h-1 bg-muted rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full ${type === 'fact' ? 'bg-blue-400' : type === 'preference' ? 'bg-purple-400' : type === 'learning' ? 'bg-emerald-400' : 'bg-amber-400'}`}
                    style={{ width: `${Math.min(100, (count / stats.total) * 100)}%` }}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Search & Filters */}
      <div className="bg-card rounded-xl border border-border shadow-sm p-4">
        <div className="flex flex-wrap gap-3 items-end">
          <div className="flex-1 min-w-[300px]">
            <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Search Memories</label>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <input
                  type="text"
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleSearch()}
                  placeholder="Search by content or keywords..."
                  className="w-full px-3 py-2.5 pl-9 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring"
                />
                <Search size={14} className="absolute left-3 top-3 text-muted-foreground" />
              </div>
              <button onClick={handleSearch} className="px-4 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 text-sm font-medium transition-colors">
                Search
              </button>
            </div>
          </div>
          <div>
            <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Type</label>
            <select value={filterType} onChange={e => setFilterType(e.target.value)} className="px-3 py-2.5 border border-border rounded-lg text-sm">
              <option value="">All Types</option>
              {TYPE_OPTIONS.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Agent</label>
            <select value={filterAgent} onChange={e => setFilterAgent(e.target.value)} className="px-3 py-2.5 border border-border rounded-lg text-sm">
              <option value="">All Agents</option>
              <option value="__global__">Global</option>
              {uniqueAgents.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          {/* View Toggle */}
          <div className="flex bg-muted rounded-lg p-0.5">
            <button
              onClick={() => setViewMode('grid')}
              className={`px-3 py-2 text-xs font-medium rounded-md transition-colors ${viewMode === 'grid' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <Layers size={14} />
            </button>
            <button
              onClick={() => setViewMode('timeline')}
              className={`px-3 py-2 text-xs font-medium rounded-md transition-colors ${viewMode === 'timeline' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <Clock size={14} />
            </button>
          </div>
          <button
            onClick={() => { setSearchQuery(''); setFilterType(''); setFilterAgent(''); loadMemories(); }}
            className="p-2.5 text-muted-foreground hover:text-muted-foreground transition-colors"
            title="Reset filters"
          >
            <RefreshCw size={16} />
          </button>
        </div>
      </div>

      {/* Memory Grid */}
      {loading ? (
        <div className="bg-card rounded-xl border border-border shadow-sm p-12 text-center">
          <Brain size={32} className="mx-auto mb-2 animate-pulse text-muted-foreground/50" />
          <p className="text-sm text-muted-foreground">Loading memories...</p>
        </div>
      ) : memories.length === 0 ? (
        <div className="bg-card rounded-xl border border-border shadow-sm p-12 text-center">
          <Brain size={40} className="mx-auto mb-3 text-muted-foreground/40" />
          <h3 className="text-sm font-semibold text-muted-foreground">No memories found</h3>
          <p className="text-xs text-muted-foreground mt-1">Create your first memory or adjust filters.</p>
        </div>
      ) : viewMode === 'grid' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {memories.map(mem => {
            const Icon = TYPE_ICONS[mem.memory_type] || Brain;
            return (
              <div key={mem.id} className={`bg-card rounded-xl border border-border shadow-sm overflow-hidden hover:shadow-md transition-shadow border-l-4 ${TYPE_BG[mem.memory_type] || 'border-l-slate-300'}`}>
                <div className="p-4">
                  {/* Header */}
                  <div className="flex items-center gap-2 mb-3">
                    <span className={`text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase border ${TYPE_COLORS[mem.memory_type] || 'bg-muted text-muted-foreground'}`}>
                      <Icon size={10} className="inline mr-1" />{mem.memory_type}
                    </span>
                    {mem.agent_key && (
                      <span className="text-[10px] bg-muted text-muted-foreground px-2 py-0.5 rounded-full font-medium">{mem.agent_key}</span>
                    )}
                    {mem.tender_id && (
                      <span className="text-[10px] bg-orange-50 dark:bg-orange-500/15 text-orange-600 dark:text-orange-400 px-2 py-0.5 rounded-full font-medium">T#{mem.tender_id}</span>
                    )}
                  </div>

                  {/* Content */}
                  <p className="text-sm text-foreground leading-relaxed line-clamp-3">{mem.content}</p>

                  {/* Context */}
                  {mem.context && (
                    <p className="text-xs text-muted-foreground mt-2 italic line-clamp-1">Context: {mem.context}</p>
                  )}

                  {/* Keywords */}
                  {mem.keywords && mem.keywords.length > 0 && (
                    <div className="flex items-center gap-1 mt-3 flex-wrap">
                      {mem.keywords.slice(0, 4).map((kw, i) => (
                        <span key={i} className="text-[10px] bg-muted/40 text-muted-foreground px-1.5 py-0.5 rounded border border-border">{kw}</span>
                      ))}
                      {mem.keywords.length > 4 && (
                        <span className="text-[10px] text-muted-foreground">+{mem.keywords.length - 4}</span>
                      )}
                    </div>
                  )}

                  {/* Importance Bar */}
                  <div className="mt-3 flex items-center gap-2">
                    <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full ${mem.importance >= 0.7 ? 'bg-emerald-400' : mem.importance >= 0.4 ? 'bg-blue-400' : 'bg-muted-foreground/40'}`}
                        style={{ width: `${mem.importance * 100}%` }}
                      />
                    </div>
                    <span className="text-[10px] text-muted-foreground shrink-0">{(mem.importance * 100).toFixed(0)}%</span>
                  </div>

                  {/* Footer */}
                  <div className="flex items-center justify-between mt-3 pt-3 border-t border-border">
                    <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
                      <span className="flex items-center gap-0.5"><Eye size={10} /> {mem.access_count}</span>
                      <span className="flex items-center gap-0.5"><Calendar size={10} /> {new Date(mem.created_at).toLocaleDateString()}</span>
                    </div>
                    <button
                      onClick={() => handleDelete(mem.id)}
                      className="p-1 text-muted-foreground/50 hover:text-red-500 transition-colors"
                      title="Delete"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        /* Timeline View */
        <div className="space-y-6">
          {Object.entries(groupedByDate).map(([date, mems]) => (
            <div key={date}>
              <div className="flex items-center gap-2 mb-3">
                <Calendar size={14} className="text-muted-foreground" />
                <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">{date}</span>
                <span className="text-xs text-muted-foreground">({mems.length})</span>
                <div className="flex-1 h-px bg-muted" />
              </div>
              <div className="space-y-2 ml-5 border-l-2 border-border pl-4">
                {mems.map(mem => (
                  <div key={mem.id} className="relative bg-card rounded-lg border border-border p-3 hover:shadow-sm transition-shadow">
                    <div className="absolute -left-[21px] top-4 w-2.5 h-2.5 rounded-full bg-card border-2 border-border" />
                    <div className="flex items-start gap-2">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${TYPE_COLORS[mem.memory_type]}`}>
                            {mem.memory_type}
                          </span>
                          {mem.agent_key && <span className="text-[10px] text-muted-foreground">{mem.agent_key}</span>}
                          <span className="text-[10px] text-muted-foreground ml-auto">{new Date(mem.created_at).toLocaleTimeString()}</span>
                        </div>
                        <p className="text-sm text-foreground">{mem.content}</p>
                      </div>
                      <button onClick={() => handleDelete(mem.id)} className="p-1 text-muted-foreground/50 hover:text-red-500 transition-colors shrink-0">
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Add Memory Modal */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowAddModal(false)}>
          <div className="bg-card rounded-xl shadow-xl w-full max-w-lg" onClick={e => e.stopPropagation()}>
            <div className="px-6 py-4 border-b border-border">
              <h2 className="text-lg font-semibold text-foreground">Add Memory</h2>
              <p className="text-xs text-muted-foreground mt-0.5">Store a new memory for agents to reference</p>
            </div>
            <div className="px-6 py-4 space-y-4">
              {/* Type Selector */}
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Memory Type</label>
                <div className="grid grid-cols-4 gap-2">
                  {TYPE_OPTIONS.map(type => {
                    const Icon = TYPE_ICONS[type] || Brain;
                    const isSelected = newMemory.memory_type === type;
                    return (
                      <button
                        key={type}
                        onClick={() => setNewMemory(p => ({ ...p, memory_type: type }))}
                        className={`flex flex-col items-center gap-1 p-3 rounded-lg border-2 text-xs font-medium transition-all ${
                          isSelected
                            ? `${TYPE_COLORS[type]} border-current`
                            : 'border-border text-muted-foreground hover:border-border'
                        }`}
                      >
                        <Icon size={16} />
                        <span className="capitalize">{type}</span>
                      </button>
                    );
                  })}
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Agent Key (optional)</label>
                <input
                  type="text"
                  value={newMemory.agent_key}
                  onChange={e => setNewMemory(p => ({ ...p, agent_key: e.target.value }))}
                  placeholder="Leave empty for global memory"
                  className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Content</label>
                <textarea
                  value={newMemory.content}
                  onChange={e => setNewMemory(p => ({ ...p, content: e.target.value }))}
                  rows={3}
                  placeholder="What should the agent remember?"
                  className="w-full px-3 py-2.5 border border-border rounded-lg text-sm resize-none focus:ring-2 focus:ring-ring"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Context (optional)</label>
                <input
                  type="text"
                  value={newMemory.context}
                  onChange={e => setNewMemory(p => ({ ...p, context: e.target.value }))}
                  placeholder="What led to this memory?"
                  className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring"
                />
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Keywords</label>
                  <input
                    type="text"
                    value={newMemory.keywords}
                    onChange={e => setNewMemory(p => ({ ...p, keywords: e.target.value }))}
                    placeholder="comma, separated"
                    className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Importance ({(newMemory.importance * 100).toFixed(0)}%)</label>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={newMemory.importance}
                    onChange={e => setNewMemory(p => ({ ...p, importance: parseFloat(e.target.value) }))}
                    className="w-full mt-1.5"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Tender ID</label>
                  <input
                    type="number"
                    value={newMemory.tender_id}
                    onChange={e => setNewMemory(p => ({ ...p, tender_id: e.target.value }))}
                    placeholder="Optional"
                    className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring"
                  />
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-3 px-6 py-4 border-t border-border bg-muted/40 rounded-b-xl">
              <button onClick={() => setShowAddModal(false)} className="px-4 py-2 text-sm text-muted-foreground hover:bg-muted rounded-lg transition-colors">Cancel</button>
              <button
                onClick={handleAdd}
                disabled={!newMemory.content.trim()}
                className="px-5 py-2 text-sm bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 disabled:opacity-50 font-medium transition-colors"
              >
                Save Memory
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Upload Knowledge File Modal */}
      {showUploadModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={resetUploadModal}>
          <div className="bg-card rounded-xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0">
              <div>
                <h2 className="text-lg font-semibold text-foreground">Upload Knowledge File</h2>
                <p className="text-xs text-muted-foreground mt-0.5">
                  {uploadStep === 'select' && 'Upload a .md file to bulk-import knowledge as agent memories'}
                  {uploadStep === 'preview' && `${parsedEntries.length} entries parsed — review before saving`}
                  {uploadStep === 'done' && `${createdCount} memories created successfully`}
                </p>
              </div>
              <button onClick={resetUploadModal} className="text-muted-foreground hover:text-muted-foreground"><X size={18} /></button>
            </div>

            <div className="px-6 py-4 space-y-4 overflow-y-auto flex-1">
              {/* Step 1: Select file */}
              {uploadStep === 'select' && (
                <>
                  {/* File picker */}
                  <div>
                    <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Markdown File</label>
                    {uploadFile ? (
                      <div className="flex items-center gap-3 p-3 bg-emerald-50 dark:bg-emerald-500/15 border border-emerald-200 dark:border-emerald-500/20 rounded-lg">
                        <FileText size={18} className="text-emerald-600 dark:text-emerald-400" />
                        <div className="flex-1">
                          <span className="text-sm font-medium text-foreground">{uploadFile.name}</span>
                          <span className="text-xs text-muted-foreground ml-2">{(uploadFile.size / 1024).toFixed(1)} KB</span>
                        </div>
                        <button onClick={() => setUploadFile(null)} className="text-muted-foreground hover:text-red-500"><X size={14} /></button>
                      </div>
                    ) : (
                      <label className="flex flex-col items-center gap-2 p-6 border-2 border-dashed border-border rounded-lg bg-muted/40 hover:bg-muted cursor-pointer transition-colors">
                        <Upload size={24} className="text-muted-foreground" />
                        <span className="text-sm text-muted-foreground">Click to select a .md or .txt file</span>
                        <span className="text-xs text-muted-foreground">Max 500KB</span>
                        <input
                          type="file"
                          accept=".md,.txt"
                          className="hidden"
                          onChange={e => { if (e.target.files?.[0]) setUploadFile(e.target.files[0]); e.target.value = ''; }}
                        />
                      </label>
                    )}
                  </div>

                  {/* Agent key */}
                  <div>
                    <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1.5">Agent Key (optional)</label>
                    <input
                      type="text"
                      value={uploadAgentKey}
                      onChange={e => setUploadAgentKey(e.target.value)}
                      placeholder="Leave empty for global memory"
                      className="w-full px-3 py-2.5 border border-border rounded-lg text-sm focus:ring-2 focus:ring-emerald-500"
                    />
                  </div>

                  {/* AI parsing toggle */}
                  <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg border border-border">
                    <div>
                      <span className="text-sm font-medium text-foreground">AI-Powered Parsing</span>
                      <p className="text-xs text-muted-foreground mt-0.5">Use Claude to structure freeform text into memory entries</p>
                    </div>
                    <button
                      onClick={() => setUseAiParsing(p => !p)}
                      className={`relative w-11 h-6 rounded-full transition-colors ${useAiParsing ? 'bg-emerald-500' : 'bg-muted-foreground/40'}`}
                    >
                      <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-card rounded-full shadow transition-transform ${useAiParsing ? 'translate-x-5' : ''}`} />
                    </button>
                  </div>

                  {!useAiParsing && (
                    <div className="bg-accent/10 border border-accent/20 rounded-lg p-3">
                      <p className="text-xs text-accent font-medium mb-1">Structured Format Expected:</p>
                      <pre className="text-[11px] text-accent font-mono whitespace-pre-wrap">{`## FACT\nYour knowledge here\nKeywords: keyword1, keyword2\nImportance: 0.8\n\n## LEARNING\nAnother piece of knowledge\nKeywords: key1, key2\nContext: Source of this learning`}</pre>
                    </div>
                  )}

                  {uploadError && (
                    <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-3 text-sm text-red-600 dark:text-red-400">{uploadError}</div>
                  )}
                </>
              )}

              {/* Step 2: Preview parsed entries */}
              {uploadStep === 'preview' && (
                <>
                  <div className="space-y-2 max-h-[50vh] overflow-y-auto">
                    {parsedEntries.map((entry, idx) => {
                      const Icon = TYPE_ICONS[entry.memory_type] || Brain;
                      return (
                        <div key={idx} className={`flex items-start gap-3 p-3 bg-card border border-border rounded-lg border-l-4 ${TYPE_BG[entry.memory_type] || 'border-l-slate-400'}`}>
                          <div className="shrink-0 mt-0.5">
                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold rounded-full border ${TYPE_COLORS[entry.memory_type] || 'bg-muted text-muted-foreground border-border'}`}>
                              <Icon size={10} /> {entry.memory_type}
                            </span>
                          </div>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm text-foreground line-clamp-2">{entry.content}</p>
                            <div className="flex items-center gap-3 mt-1.5">
                              {entry.keywords?.length > 0 && (
                                <span className="text-[10px] text-muted-foreground flex items-center gap-1">
                                  <Tag size={9} /> {entry.keywords.join(', ')}
                                </span>
                              )}
                              <span className="text-[10px] text-muted-foreground">
                                Importance: {(entry.importance * 100).toFixed(0)}%
                              </span>
                              {entry.context && (
                                <span className="text-[10px] text-muted-foreground truncate max-w-[200px]" title={entry.context}>
                                  {entry.context}
                                </span>
                              )}
                            </div>
                          </div>
                          <button
                            onClick={() => setParsedEntries(prev => prev.filter((_, i) => i !== idx))}
                            className="shrink-0 text-muted-foreground/50 hover:text-red-500 transition-colors mt-0.5"
                          >
                            <X size={14} />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                  {parsedEntries.length === 0 && (
                    <div className="text-center py-6 text-muted-foreground text-sm">All entries removed. Go back to re-parse.</div>
                  )}
                  {uploadError && (
                    <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg p-3 text-sm text-red-600 dark:text-red-400">{uploadError}</div>
                  )}
                </>
              )}

              {/* Step 3: Done */}
              {uploadStep === 'done' && (
                <div className="flex flex-col items-center py-8 gap-3">
                  <CheckCircle2 size={48} className="text-emerald-500" />
                  <h3 className="text-lg font-semibold text-foreground">{createdCount} Memories Created</h3>
                  <p className="text-sm text-muted-foreground">Knowledge has been imported and is now available to agents.</p>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex justify-between gap-3 px-6 py-4 border-t border-border bg-muted/40 rounded-b-xl shrink-0">
              {uploadStep === 'select' && (
                <>
                  <button onClick={resetUploadModal} className="px-4 py-2 text-sm text-muted-foreground hover:bg-muted rounded-lg transition-colors">Cancel</button>
                  <button
                    onClick={handleParseMd}
                    disabled={!uploadFile || uploading}
                    className="px-5 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 font-medium transition-colors flex items-center gap-2"
                  >
                    {uploading ? <><Loader2 size={14} className="animate-spin" /> {useAiParsing ? 'AI Parsing...' : 'Parsing...'}</> : 'Parse File'}
                  </button>
                </>
              )}
              {uploadStep === 'preview' && (
                <>
                  <button onClick={() => { setUploadStep('select'); setUploadError(''); }} className="px-4 py-2 text-sm text-muted-foreground hover:bg-muted rounded-lg transition-colors">Back</button>
                  <button
                    onClick={handleCommitUpload}
                    disabled={parsedEntries.length === 0 || uploading}
                    className="px-5 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 font-medium transition-colors flex items-center gap-2"
                  >
                    {uploading ? <><Loader2 size={14} className="animate-spin" /> Saving...</> : `Save All (${parsedEntries.length})`}
                  </button>
                </>
              )}
              {uploadStep === 'done' && (
                <button onClick={resetUploadModal} className="ml-auto px-5 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 font-medium transition-colors">Close</button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
