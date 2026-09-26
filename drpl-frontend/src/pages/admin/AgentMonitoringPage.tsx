import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Activity, BarChart3, Clock, DollarSign, CheckCircle, XCircle, AlertCircle } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getBuilderAgent, getAgentExecutions, getAgentStats, getAgentExecutionDetail } from '../../lib/api';
import { formatDateTime } from '../../lib/formatters';

const STATUS_STYLES: Record<string, string> = {
  running: 'bg-accent/10 text-accent',
  completed: 'bg-emerald-50 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
  failed: 'bg-red-50 dark:bg-red-500/15 text-red-600 dark:text-red-400',
  timeout: 'bg-amber-50 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400',
};

const STATUS_ICONS: Record<string, any> = {
  running: Activity,
  completed: CheckCircle,
  failed: XCircle,
  timeout: AlertCircle,
};

export default function AgentMonitoringPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const agentId = Number(id);

  const [agentName, setAgentName] = useState('');
  const [stats, setStats] = useState<any>(null);
  const [executions, setExecutions] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [expandedDetail, setExpandedDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const [agent, st, execs] = await Promise.all([
          getBuilderAgent(agentId).catch(() => null),
          getAgentStats(agentId).catch(() => null),
          getAgentExecutions(agentId).catch(() => []),
        ]);
        setAgentName(agent?.display_name || 'Agent');
        setStats(st);
        setExecutions(execs);
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [agentId]);

  const handleExpand = async (execId: number) => {
    if (expandedId === execId) {
      setExpandedId(null);
      setExpandedDetail(null);
      return;
    }
    setExpandedId(execId);
    setDetailLoading(true);
    try {
      const detail = await getAgentExecutionDetail(execId);
      setExpandedDetail(detail);
    } catch {
      setExpandedDetail(null);
    } finally {
      setDetailLoading(false);
    }
  };

  if (loading) return <><Header title="Agent Monitoring" /><LoadingSpinner /></>;

  return (
    <>
      <Header title={`Monitoring: ${agentName}`} />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Back link */}
        <div>
          <button
            onClick={() => navigate(`/admin/agent-builder/${id}`)}
            className="text-sm text-muted-foreground hover:text-drpl-primary"
          >
            Back to Editor
          </button>
        </div>

        {/* Stats cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="bg-card rounded-xl shadow-card border border-border p-5">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-accent flex items-center justify-center">
                <BarChart3 size={18} className="text-white" />
              </div>
              <div>
                <p className="text-2xl font-bold text-foreground">{stats?.total_executions ?? 0}</p>
                <p className="text-xs text-muted-foreground">Total Executions</p>
              </div>
            </div>
          </div>
          <div className="bg-card rounded-xl shadow-card border border-border p-5">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-emerald-500 flex items-center justify-center">
                <CheckCircle size={18} className="text-white" />
              </div>
              <div>
                <p className="text-2xl font-bold text-foreground">
                  {stats?.success_rate != null ? `${(stats.success_rate * 100).toFixed(1)}%` : '-'}
                </p>
                <p className="text-xs text-muted-foreground">Success Rate</p>
              </div>
            </div>
          </div>
          <div className="bg-card rounded-xl shadow-card border border-border p-5">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-amber-500 flex items-center justify-center">
                <Clock size={18} className="text-white" />
              </div>
              <div>
                <p className="text-2xl font-bold text-foreground">
                  {stats?.avg_latency_ms != null ? `${Math.round(stats.avg_latency_ms)}ms` : '-'}
                </p>
                <p className="text-xs text-muted-foreground">Avg Latency</p>
              </div>
            </div>
          </div>
          <div className="bg-card rounded-xl shadow-card border border-border p-5">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-purple-500 flex items-center justify-center">
                <DollarSign size={18} className="text-white" />
              </div>
              <div>
                <p className="text-2xl font-bold text-foreground">
                  {stats?.total_cost != null ? `$${stats.total_cost.toFixed(2)}` : '-'}
                </p>
                <p className="text-xs text-muted-foreground">Total Cost</p>
              </div>
            </div>
          </div>
        </div>

        {/* Execution history */}
        <div className="bg-card rounded-xl shadow-card border border-border">
          <div className="px-5 py-4 border-b border-border">
            <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
              <Activity size={16} /> Execution History
            </h3>
          </div>

          {executions.length === 0 ? (
            <div className="text-center py-16 text-muted-foreground">
              <Activity size={40} className="mx-auto mb-2 opacity-40" />
              <p className="text-sm">No executions recorded yet.</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-muted-foreground border-b border-border">
                    <th className="px-5 py-3 font-medium">Status</th>
                    <th className="px-5 py-3 font-medium">Trigger</th>
                    <th className="px-5 py-3 font-medium">Input</th>
                    <th className="px-5 py-3 font-medium">Output</th>
                    <th className="px-5 py-3 font-medium">Tokens</th>
                    <th className="px-5 py-3 font-medium">Latency</th>
                    <th className="px-5 py-3 font-medium">Cost</th>
                    <th className="px-5 py-3 font-medium">Timestamp</th>
                  </tr>
                </thead>
                <tbody>
                  {executions.map((exec) => {
                    const StatusIcon = STATUS_ICONS[exec.status] || Activity;
                    const inputPreview = typeof exec.input_data === 'string'
                      ? exec.input_data
                      : JSON.stringify(exec.input_data || {});
                    const outputPreview = typeof exec.output === 'string'
                      ? exec.output
                      : JSON.stringify(exec.output || {});
                    return (
                      <tr
                        key={exec.id}
                        className="border-b border-border hover:bg-muted/40/50 cursor-pointer"
                        onClick={() => handleExpand(exec.id)}
                      >
                        <td className="px-5 py-3">
                          <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_STYLES[exec.status] || 'bg-muted text-muted-foreground'}`}>
                            <StatusIcon size={12} /> {exec.status}
                          </span>
                        </td>
                        <td className="px-5 py-3 text-muted-foreground">{exec.trigger || '-'}</td>
                        <td className="px-5 py-3 text-muted-foreground max-w-[160px] truncate">{inputPreview}</td>
                        <td className="px-5 py-3 text-muted-foreground max-w-[160px] truncate">{outputPreview}</td>
                        <td className="px-5 py-3 text-muted-foreground">{exec.tokens_used ?? '-'}</td>
                        <td className="px-5 py-3 text-muted-foreground">{exec.latency_ms != null ? `${exec.latency_ms}ms` : '-'}</td>
                        <td className="px-5 py-3 text-muted-foreground">{exec.cost != null ? `$${exec.cost.toFixed(4)}` : '-'}</td>
                        <td className="px-5 py-3 text-muted-foreground text-xs">{formatDateTime(exec.created_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Expanded detail */}
          {expandedId && (
            <div className="px-5 py-4 bg-muted/40 border-t border-border">
              {detailLoading ? (
                <LoadingSpinner />
              ) : expandedDetail ? (
                <div className="space-y-3">
                  <h4 className="text-xs font-semibold text-muted-foreground">Execution Detail (ID: {expandedId})</h4>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <p className="text-[11px] text-muted-foreground mb-1">Full Input</p>
                      <pre className="text-xs font-mono bg-card rounded p-3 border border-border overflow-auto max-h-48">
                        {JSON.stringify(expandedDetail.input_data, null, 2)}
                      </pre>
                    </div>
                    <div>
                      <p className="text-[11px] text-muted-foreground mb-1">Full Output</p>
                      <pre className="text-xs font-mono bg-card rounded p-3 border border-border overflow-auto max-h-48">
                        {typeof expandedDetail.output === 'string' ? expandedDetail.output : JSON.stringify(expandedDetail.output, null, 2)}
                      </pre>
                    </div>
                  </div>
                  {expandedDetail.error && (
                    <div>
                      <p className="text-[11px] text-red-400 mb-1">Error</p>
                      <pre className="text-xs font-mono bg-red-50 dark:bg-red-500/15 text-red-600 dark:text-red-400 rounded p-3 border border-red-200 dark:border-red-500/20 overflow-auto max-h-32">
                        {expandedDetail.error}
                      </pre>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">Could not load execution details.</p>
              )}
            </div>
          )}

          {/* Load more */}
          {executions.length >= 20 && (
            <div className="px-5 py-3 text-center border-t border-border">
              <button className="text-sm text-drpl-primary hover:underline">Load More</button>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
