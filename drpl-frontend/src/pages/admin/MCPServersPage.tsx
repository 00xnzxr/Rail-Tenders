import { useState, useEffect, useCallback } from 'react';
import {
  Server, Plus, Trash2, RefreshCw, CheckCircle2, XCircle, Loader2,
  Wrench, Edit2, Power, PowerOff, Terminal, Globe, Radio, X,
  ChevronRight, Wifi, WifiOff, Code2, Info, Shield,
} from 'lucide-react';
import {
  getMCPServers, addMCPServer, updateMCPServer, deleteMCPServer,
  testMCPServer, getMCPServerTools,
} from '../../lib/api';

interface MCPServer {
  id: number;
  server_name: string;
  display_name: string;
  description?: string;
  transport_type: 'stdio' | 'sse' | 'http';
  command?: string;
  args?: string[];
  url?: string;
  env_vars?: Record<string, string>;
  is_enabled: boolean;
  created_at: string;
}

interface MCPTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, any>;
}

const TRANSPORT_CONFIG: Record<string, { icon: any; label: string; color: string; bg: string; description: string }> = {
  stdio: {
    icon: Terminal,
    label: 'STDIO',
    color: 'text-purple-700 dark:text-purple-400',
    bg: 'bg-purple-50 dark:bg-purple-500/15 border-purple-200 dark:border-purple-500/20',
    description: 'Local process via stdin/stdout',
  },
  sse: {
    icon: Radio,
    label: 'SSE',
    color: 'text-accent',
    bg: 'bg-accent/10 border-accent/20',
    description: 'Server-Sent Events stream',
  },
  http: {
    icon: Globe,
    label: 'HTTP',
    color: 'text-emerald-700 dark:text-emerald-400',
    bg: 'bg-emerald-50 dark:bg-emerald-500/15 border-emerald-200 dark:border-emerald-500/20',
    description: 'HTTP/REST endpoint',
  },
};

export default function MCPServersPage() {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAddModal, setShowAddModal] = useState(false);
  const [editingServer, setEditingServer] = useState<MCPServer | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<Record<number, 'connected' | 'error' | 'testing'>>({});
  const [testMessages, setTestMessages] = useState<Record<number, string>>({});
  const [toolsPanel, setToolsPanel] = useState<{ server: MCPServer; tools: MCPTool[] } | null>(null);
  const [loadingTools, setLoadingTools] = useState(false);
  const [expandedTool, setExpandedTool] = useState<string | null>(null);

  const [form, setForm] = useState({
    server_name: '',
    display_name: '',
    description: '',
    transport_type: 'stdio' as 'stdio' | 'sse' | 'http',
    command: '',
    args: '',
    url: '',
    env_vars: '',
    is_enabled: true,
  });

  const loadServers = async () => {
    setLoading(true);
    try {
      const data = await getMCPServers();
      setServers(data);
    } catch { /* ignore */ }
    setLoading(false);
  };

  useEffect(() => { loadServers(); }, []);

  // Auto-test enabled servers on load
  const autoTestServers = useCallback(async (serverList: MCPServer[]) => {
    const enabled = serverList.filter(s => s.is_enabled);
    enabled.forEach(server => {
      setConnectionStatus(prev => ({ ...prev, [server.id]: 'testing' }));
      testMCPServer(server.id)
        .then((result: any) => {
          setConnectionStatus(prev => ({ ...prev, [server.id]: 'connected' }));
          setTestMessages(prev => ({ ...prev, [server.id]: result.message || 'Connected' }));
        })
        .catch((err: any) => {
          setConnectionStatus(prev => ({ ...prev, [server.id]: 'error' }));
          setTestMessages(prev => ({ ...prev, [server.id]: err.response?.data?.detail || err.message }));
        });
    });
  }, []);

  useEffect(() => {
    if (servers.length > 0) autoTestServers(servers);
  }, [servers.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const resetForm = () => {
    setForm({
      server_name: '', display_name: '', description: '',
      transport_type: 'stdio', command: '', args: '', url: '', env_vars: '', is_enabled: true,
    });
    setEditingServer(null);
  };

  const openEdit = (server: MCPServer) => {
    setForm({
      server_name: server.server_name,
      display_name: server.display_name,
      description: server.description || '',
      transport_type: server.transport_type,
      command: server.command || '',
      args: server.args ? server.args.join(', ') : '',
      url: server.url || '',
      env_vars: server.env_vars ? Object.entries(server.env_vars).map(([k, v]) => `${k}=${v}`).join('\n') : '',
      is_enabled: server.is_enabled,
    });
    setEditingServer(server);
    setShowAddModal(true);
  };

  const parseEnvVars = (text: string): Record<string, string> => {
    const vars: Record<string, string> = {};
    text.split('\n').filter(Boolean).forEach(line => {
      const [key, ...rest] = line.split('=');
      if (key?.trim()) vars[key.trim()] = rest.join('=').trim();
    });
    return vars;
  };

  const handleSave = async () => {
    const body: Record<string, any> = {
      server_name: form.server_name,
      display_name: form.display_name,
      description: form.description || undefined,
      transport_type: form.transport_type,
      is_enabled: form.is_enabled,
    };
    if (form.transport_type === 'stdio') {
      body.command = form.command;
      body.args = form.args ? form.args.split(',').map(a => a.trim()) : [];
    } else {
      body.url = form.url;
    }
    if (form.env_vars.trim()) {
      body.env_vars = parseEnvVars(form.env_vars);
    }

    try {
      if (editingServer) {
        await updateMCPServer(editingServer.id, body);
      } else {
        await addMCPServer(body);
      }
      setShowAddModal(false);
      resetForm();
      loadServers();
    } catch { /* ignore */ }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this MCP server configuration?')) return;
    try {
      await deleteMCPServer(id);
      setServers(prev => prev.filter(s => s.id !== id));
      if (toolsPanel?.server.id === id) setToolsPanel(null);
    } catch { /* ignore */ }
  };

  const handleTest = async (server: MCPServer) => {
    setConnectionStatus(prev => ({ ...prev, [server.id]: 'testing' }));
    try {
      const result = await testMCPServer(server.id);
      setConnectionStatus(prev => ({ ...prev, [server.id]: 'connected' }));
      setTestMessages(prev => ({ ...prev, [server.id]: result.message || 'Connected successfully' }));
    } catch (err: any) {
      setConnectionStatus(prev => ({ ...prev, [server.id]: 'error' }));
      setTestMessages(prev => ({ ...prev, [server.id]: err.response?.data?.detail || err.message }));
    }
  };

  const handleLoadTools = async (server: MCPServer) => {
    if (toolsPanel?.server.id === server.id) {
      setToolsPanel(null);
      return;
    }
    setLoadingTools(true);
    setToolsPanel({ server, tools: [] });
    try {
      const data = await getMCPServerTools(server.id);
      setToolsPanel({ server, tools: data.tools || data || [] });
    } catch {
      setToolsPanel({ server, tools: [] });
    }
    setLoadingTools(false);
  };

  const handleToggle = async (server: MCPServer) => {
    try {
      await updateMCPServer(server.id, { is_enabled: !server.is_enabled });
      setServers(prev => prev.map(s => s.id === server.id ? { ...s, is_enabled: !s.is_enabled } : s));
    } catch { /* ignore */ }
  };

  const enabledCount = servers.filter(s => s.is_enabled).length;
  const connectedCount = Object.values(connectionStatus).filter(s => s === 'connected').length;
  const totalTools = toolsPanel?.tools.length || 0;

  const ConnectionDot = ({ serverId }: { serverId: number }) => {
    const status = connectionStatus[serverId];
    if (status === 'testing') return <Loader2 size={10} className="animate-spin text-accent" />;
    if (status === 'connected') return <span className="w-2.5 h-2.5 rounded-full bg-green-500 inline-block" />;
    if (status === 'error') return <span className="w-2.5 h-2.5 rounded-full bg-red-500 inline-block" />;
    return <span className="w-2.5 h-2.5 rounded-full bg-muted-foreground/40 inline-block" />;
  };

  return (
    <div className="mx-auto w-full max-w-[1600px] p-4 sm:p-6 lg:p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-foreground">MCP Servers</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Configure external Model Context Protocol servers for agent tool access
          </p>
        </div>
        <button
          onClick={() => { resetForm(); setShowAddModal(true); }}
          className="flex items-center gap-2 px-4 py-2.5 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 text-sm font-medium transition-colors shadow-sm"
        >
          <Plus size={16} /> Add Server
        </button>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="bg-card rounded-xl border border-border shadow-sm p-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-muted/40 flex items-center justify-center">
              <Server size={20} className="text-muted-foreground" />
            </div>
            <div>
              <p className="text-2xl font-bold text-foreground">{servers.length}</p>
              <p className="text-xs text-muted-foreground">Total Servers</p>
            </div>
          </div>
        </div>
        <div className="bg-card rounded-xl border border-border shadow-sm p-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-green-50 dark:bg-green-500/15 flex items-center justify-center">
              <Wifi size={20} className="text-green-600 dark:text-green-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-foreground">{connectedCount}<span className="text-sm font-normal text-muted-foreground">/{enabledCount}</span></p>
              <p className="text-xs text-muted-foreground">Connected</p>
            </div>
          </div>
        </div>
        <div className="bg-card rounded-xl border border-border shadow-sm p-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-purple-50 dark:bg-purple-500/15 flex items-center justify-center">
              <Wrench size={20} className="text-purple-600 dark:text-purple-400" />
            </div>
            <div>
              <p className="text-2xl font-bold text-foreground">{totalTools}</p>
              <p className="text-xs text-muted-foreground">Tools Loaded</p>
            </div>
          </div>
        </div>
      </div>

      {/* Main Layout */}
      <div className={`flex gap-6 ${toolsPanel ? '' : ''}`}>
        {/* Server Cards Grid */}
        <div className={`${toolsPanel ? 'flex-1 min-w-0' : 'w-full'}`}>
          {loading ? (
            <div className="bg-card rounded-xl border border-border shadow-sm p-12 text-center text-muted-foreground">
              <Server size={32} className="mx-auto mb-2 animate-pulse" />
              <p className="text-sm">Loading servers...</p>
            </div>
          ) : servers.length === 0 ? (
            <div className="bg-card rounded-xl border border-border shadow-sm p-12 text-center">
              <div className="w-16 h-16 rounded-2xl bg-muted/40 flex items-center justify-center mx-auto mb-4">
                <Server size={28} className="text-muted-foreground/50" />
              </div>
              <p className="text-sm font-medium text-muted-foreground">No MCP servers configured</p>
              <p className="text-xs text-muted-foreground mt-1 mb-4">Add an external MCP server to extend agent capabilities</p>
              <button
                onClick={() => { resetForm(); setShowAddModal(true); }}
                className="inline-flex items-center gap-2 px-4 py-2 bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 text-sm font-medium transition-colors"
              >
                <Plus size={14} /> Add Your First Server
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              {servers.map(server => {
                const transport = TRANSPORT_CONFIG[server.transport_type];
                const TransportIcon = transport.icon;
                const status = connectionStatus[server.id];

                return (
                  <div
                    key={server.id}
                    className={`bg-card rounded-xl border shadow-sm transition-all hover:shadow-md ${
                      toolsPanel?.server.id === server.id ? 'border-purple-300 ring-1 ring-purple-100' : 'border-border'
                    }`}
                  >
                    <div className="p-5">
                      {/* Top Row: Status + Toggle */}
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-2">
                          <ConnectionDot serverId={server.id} />
                          <span className={`text-xs font-medium ${
                            status === 'connected' ? 'text-green-600 dark:text-green-400' :
                            status === 'error' ? 'text-red-600 dark:text-red-400' :
                            status === 'testing' ? 'text-accent' :
                            'text-muted-foreground'
                          }`}>
                            {status === 'connected' ? 'Connected' :
                             status === 'error' ? 'Error' :
                             status === 'testing' ? 'Testing...' :
                             'Unknown'}
                          </span>
                        </div>
                        <button
                          onClick={() => handleToggle(server)}
                          className={`relative w-10 h-5 rounded-full transition-colors ${
                            server.is_enabled ? 'bg-green-500' : 'bg-muted-foreground/40'
                          }`}
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-card shadow transition-transform ${
                            server.is_enabled ? 'translate-x-5' : 'translate-x-0.5'
                          }`} />
                        </button>
                      </div>

                      {/* Server Info */}
                      <div className="flex items-start gap-3 mb-3">
                        <div className={`w-11 h-11 rounded-xl flex items-center justify-center flex-shrink-0 border ${transport.bg}`}>
                          <TransportIcon size={20} className={transport.color} />
                        </div>
                        <div className="min-w-0 flex-1">
                          <h3 className="font-semibold text-foreground truncate">{server.display_name}</h3>
                          <p className="text-xs text-muted-foreground font-mono truncate">{server.server_name}</p>
                        </div>
                        <span className={`text-[10px] px-2 py-0.5 rounded-full font-semibold border flex-shrink-0 ${transport.bg} ${transport.color}`}>
                          {transport.label}
                        </span>
                      </div>

                      {server.description && (
                        <p className="text-xs text-muted-foreground mb-3 line-clamp-2">{server.description}</p>
                      )}

                      {/* Connection Details */}
                      <div className="mb-3">
                        {server.transport_type === 'stdio' && server.command && (
                          <div className="flex items-center gap-2 bg-muted/40 rounded-lg px-3 py-2">
                            <Terminal size={12} className="text-muted-foreground flex-shrink-0" />
                            <p className="text-xs font-mono text-muted-foreground truncate">
                              {server.command} {server.args?.join(' ') || ''}
                            </p>
                          </div>
                        )}
                        {server.transport_type !== 'stdio' && server.url && (
                          <div className="flex items-center gap-2 bg-muted/40 rounded-lg px-3 py-2">
                            <Globe size={12} className="text-muted-foreground flex-shrink-0" />
                            <p className="text-xs font-mono text-muted-foreground truncate">{server.url}</p>
                          </div>
                        )}
                      </div>

                      {/* Error Message */}
                      {status === 'error' && testMessages[server.id] && (
                        <div className="flex items-start gap-2 text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-500/15 rounded-lg px-3 py-2 mb-3">
                          <XCircle size={14} className="flex-shrink-0 mt-0.5" />
                          <span className="line-clamp-2">{testMessages[server.id]}</span>
                        </div>
                      )}

                      {/* Actions */}
                      <div className="flex items-center gap-1 pt-2 border-t border-border">
                        <button
                          onClick={() => handleTest(server)}
                          disabled={connectionStatus[server.id] === 'testing'}
                          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-accent hover:bg-accent/10 rounded-lg transition-colors disabled:opacity-50"
                        >
                          {connectionStatus[server.id] === 'testing' ? (
                            <Loader2 size={13} className="animate-spin" />
                          ) : (
                            <RefreshCw size={13} />
                          )}
                          Test
                        </button>
                        <button
                          onClick={() => handleLoadTools(server)}
                          className={`flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                            toolsPanel?.server.id === server.id
                              ? 'text-purple-700 dark:text-purple-400 bg-purple-50 dark:bg-purple-500/15'
                              : 'text-purple-600 dark:text-purple-400 hover:bg-purple-50 dark:bg-purple-500/15'
                          }`}
                        >
                          <Wrench size={13} />
                          Tools
                        </button>
                        <div className="flex-1" />
                        <button
                          onClick={() => openEdit(server)}
                          className="p-1.5 rounded-lg text-muted-foreground hover:text-muted-foreground hover:bg-muted transition-colors"
                          title="Edit"
                        >
                          <Edit2 size={14} />
                        </button>
                        <button
                          onClick={() => handleDelete(server.id)}
                          className="p-1.5 rounded-lg text-muted-foreground/50 hover:text-red-500 hover:bg-red-50 dark:bg-red-500/15 transition-colors"
                          title="Delete"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Tools Slide-in Panel */}
        {toolsPanel && (
          <div className="w-96 flex-shrink-0 bg-card rounded-xl border border-border shadow-sm overflow-hidden self-start sticky top-6">
            {/* Panel Header */}
            <div className="p-4 border-b border-border bg-muted/40">
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2">
                  <Wrench size={16} className="text-purple-600 dark:text-purple-400" />
                  <h3 className="font-semibold text-foreground text-sm">Tool Browser</h3>
                </div>
                <button
                  onClick={() => setToolsPanel(null)}
                  className="p-1 rounded-md hover:bg-muted text-muted-foreground transition-colors"
                >
                  <X size={16} />
                </button>
              </div>
              <p className="text-xs text-muted-foreground">
                {toolsPanel.server.display_name} — {toolsPanel.tools.length} tool{toolsPanel.tools.length !== 1 ? 's' : ''}
              </p>
            </div>

            {/* Tools List */}
            <div className="max-h-[calc(100vh-300px)] overflow-y-auto">
              {loadingTools ? (
                <div className="p-8 text-center">
                  <Loader2 size={24} className="mx-auto animate-spin text-purple-400 mb-2" />
                  <p className="text-xs text-muted-foreground">Loading tools...</p>
                </div>
              ) : toolsPanel.tools.length === 0 ? (
                <div className="p-8 text-center">
                  <WifiOff size={24} className="mx-auto text-muted-foreground/50 mb-2" />
                  <p className="text-xs text-muted-foreground font-medium">No tools available</p>
                  <p className="text-xs text-muted-foreground mt-1">Server may be offline or has no tools</p>
                </div>
              ) : (
                <div className="p-3 space-y-2">
                  {toolsPanel.tools.map((tool, i) => (
                    <div
                      key={i}
                      className="border border-border rounded-lg hover:border-border transition-colors"
                    >
                      <button
                        onClick={() => setExpandedTool(expandedTool === tool.name ? null : tool.name)}
                        className="w-full text-left p-3"
                      >
                        <div className="flex items-center gap-2">
                          <Code2 size={14} className="text-purple-500 flex-shrink-0" />
                          <span className="text-sm font-medium text-foreground truncate">{tool.name}</span>
                          <ChevronRight size={14} className={`text-muted-foreground/50 flex-shrink-0 transition-transform ${expandedTool === tool.name ? 'rotate-90' : ''}`} />
                        </div>
                        {tool.description && (
                          <p className="text-xs text-muted-foreground mt-1 line-clamp-2 ml-[22px]">{tool.description}</p>
                        )}
                      </button>

                      {/* Expanded Tool Schema */}
                      {expandedTool === tool.name && tool.inputSchema && (
                        <div className="px-3 pb-3 border-t border-border">
                          <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mt-2 mb-1.5">Input Schema</p>
                          <div className="bg-muted/40 rounded-md p-2 overflow-x-auto">
                            <pre className="text-[11px] text-muted-foreground font-mono whitespace-pre-wrap">
                              {JSON.stringify(tool.inputSchema, null, 2)}
                            </pre>
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Add/Edit Modal */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => { setShowAddModal(false); resetForm(); }}>
          <div className="bg-card rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-hidden flex flex-col" onClick={e => e.stopPropagation()}>
            {/* Modal Header */}
            <div className="px-6 py-4 border-b border-border">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 rounded-lg bg-accent/10 flex items-center justify-center">
                    <Server size={18} className="text-accent" />
                  </div>
                  <div>
                    <h2 className="text-lg font-semibold text-foreground">
                      {editingServer ? 'Edit MCP Server' : 'Add MCP Server'}
                    </h2>
                    <p className="text-xs text-muted-foreground">Configure an external tool server</p>
                  </div>
                </div>
                <button
                  onClick={() => { setShowAddModal(false); resetForm(); }}
                  className="p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
                >
                  <X size={18} />
                </button>
              </div>
            </div>

            {/* Modal Body */}
            <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
              {/* Names */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Identification</p>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-muted-foreground mb-1">Server Key</label>
                    <input
                      type="text"
                      value={form.server_name}
                      onChange={e => setForm(p => ({ ...p, server_name: e.target.value }))}
                      placeholder="my-mcp-server"
                      className="w-full px-3 py-2 border border-border rounded-lg text-sm font-mono focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-muted-foreground mb-1">Display Name</label>
                    <input
                      type="text"
                      value={form.display_name}
                      onChange={e => setForm(p => ({ ...p, display_name: e.target.value }))}
                      placeholder="My MCP Server"
                      className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                    />
                  </div>
                </div>
                <div className="mt-3">
                  <label className="block text-sm font-medium text-muted-foreground mb-1">Description</label>
                  <input
                    type="text"
                    value={form.description}
                    onChange={e => setForm(p => ({ ...p, description: e.target.value }))}
                    placeholder="What does this server provide?"
                    className="w-full px-3 py-2 border border-border rounded-lg text-sm focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                  />
                </div>
              </div>

              {/* Transport Type Radio Cards */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Transport</p>
                <div className="grid grid-cols-3 gap-3">
                  {(['stdio', 'sse', 'http'] as const).map(t => {
                    const cfg = TRANSPORT_CONFIG[t];
                    const TIcon = cfg.icon;
                    const selected = form.transport_type === t;
                    return (
                      <button
                        key={t}
                        type="button"
                        onClick={() => setForm(p => ({ ...p, transport_type: t }))}
                        className={`flex flex-col items-center gap-1.5 p-3 rounded-xl border-2 transition-all ${
                          selected
                            ? 'border-accent bg-accent/10 shadow-sm'
                            : 'border-border hover:border-border bg-card'
                        }`}
                      >
                        <TIcon size={20} className={selected ? 'text-accent' : 'text-muted-foreground'} />
                        <span className={`text-sm font-semibold ${selected ? 'text-accent' : 'text-muted-foreground'}`}>
                          {cfg.label}
                        </span>
                        <span className="text-[10px] text-muted-foreground text-center leading-tight">{cfg.description}</span>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Dynamic Connection Fields */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">Connection</p>
                {form.transport_type === 'stdio' ? (
                  <div className="space-y-3">
                    <div>
                      <label className="block text-sm font-medium text-muted-foreground mb-1">Command</label>
                      <input
                        type="text"
                        value={form.command}
                        onChange={e => setForm(p => ({ ...p, command: e.target.value }))}
                        placeholder="npx, python, node, etc."
                        className="w-full px-3 py-2 border border-border rounded-lg text-sm font-mono focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium text-muted-foreground mb-1">Arguments (comma-separated)</label>
                      <input
                        type="text"
                        value={form.args}
                        onChange={e => setForm(p => ({ ...p, args: e.target.value }))}
                        placeholder="-m, mcp_server, --port, 8080"
                        className="w-full px-3 py-2 border border-border rounded-lg text-sm font-mono focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                      />
                    </div>
                  </div>
                ) : (
                  <div>
                    <label className="block text-sm font-medium text-muted-foreground mb-1">
                      {form.transport_type === 'sse' ? 'SSE Endpoint URL' : 'HTTP Endpoint URL'}
                    </label>
                    <input
                      type="text"
                      value={form.url}
                      onChange={e => setForm(p => ({ ...p, url: e.target.value }))}
                      placeholder={form.transport_type === 'sse' ? 'http://localhost:8001/sse' : 'http://localhost:8001/mcp'}
                      className="w-full px-3 py-2 border border-border rounded-lg text-sm font-mono focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                    />
                  </div>
                )}
              </div>

              {/* Environment Variables */}
              <div>
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3 flex items-center gap-1.5">
                  <Shield size={12} />
                  Environment Variables
                </p>
                <textarea
                  value={form.env_vars}
                  onChange={e => setForm(p => ({ ...p, env_vars: e.target.value }))}
                  rows={3}
                  placeholder={"API_KEY=sk-xxx\nDB_URL=sqlite:///..."}
                  className="w-full px-3 py-2 border border-border rounded-lg text-sm font-mono resize-none focus:ring-2 focus:ring-ring focus:border-ring outline-none"
                />
                <p className="text-[10px] text-muted-foreground mt-1">One KEY=VALUE pair per line. Values are stored securely.</p>
              </div>

              {/* Enable Toggle */}
              <div className="flex items-center justify-between p-3 bg-muted/40 rounded-lg">
                <div className="flex items-center gap-2">
                  <Power size={16} className={form.is_enabled ? 'text-green-600 dark:text-green-400' : 'text-muted-foreground'} />
                  <span className="text-sm font-medium text-foreground">Enable this server</span>
                </div>
                <button
                  type="button"
                  onClick={() => setForm(p => ({ ...p, is_enabled: !p.is_enabled }))}
                  className={`relative w-10 h-5 rounded-full transition-colors ${
                    form.is_enabled ? 'bg-green-500' : 'bg-muted-foreground/40'
                  }`}
                >
                  <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-card shadow transition-transform ${
                    form.is_enabled ? 'translate-x-5' : 'translate-x-0.5'
                  }`} />
                </button>
              </div>
            </div>

            {/* Modal Footer */}
            <div className="px-6 py-4 border-t border-border bg-muted/40 flex justify-end gap-3">
              <button
                onClick={() => { setShowAddModal(false); resetForm(); }}
                className="px-4 py-2 text-sm text-muted-foreground hover:bg-muted rounded-lg transition-colors font-medium"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={!form.server_name.trim() || !form.display_name.trim()}
                className="px-5 py-2 text-sm bg-accent text-accent-foreground rounded-lg hover:bg-accent/90 disabled:opacity-50 font-medium transition-colors shadow-sm"
              >
                {editingServer ? 'Update Server' : 'Add Server'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
