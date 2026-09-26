import { useState, useEffect } from 'react';
import { Download, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getAuditLogs, getAuditActions, exportAuditLogs } from '../../lib/api';
import { formatDateTime } from '../../lib/formatters';

interface AuditEntry {
  id: number;
  user_id: number | null;
  user_email: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  details: Record<string, any> | null;
  ip_address: string | null;
  created_at: string | null;
}

const ACTION_COLORS: Record<string, string> = {
  'user.login': 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  'user.created': 'bg-accent/15 text-accent',
  'user.updated': 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
  'user.activated': 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  'user.deactivated': 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
  'user.password_reset': 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-400',
  'setting.updated': 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  'setting.reset': 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  'agent.updated': 'bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400',
  'agent.reset': 'bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400',
  'proposal.submitted': 'bg-accent/15 text-accent',
  'proposal.approved': 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400',
  'proposal.rejected': 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
  'proposal.changes_requested': 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
  'redaction_rule.created': 'bg-teal-100 dark:bg-teal-500/20 text-teal-700 dark:text-teal-400',
  'redaction_rule.updated': 'bg-teal-100 dark:bg-teal-500/20 text-teal-700 dark:text-teal-400',
  'redaction_rule.deleted': 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
};

export default function AuditLogPage() {
  const [logs, setLogs] = useState<AuditEntry[]>([]);
  const [actions, setActions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [filters, setFilters] = useState({ action: '', date_from: '', date_to: '' });
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 30;

  const fetchLogs = () => {
    setLoading(true);
    const params: Record<string, any> = { limit: PAGE_SIZE, offset: page * PAGE_SIZE };
    if (filters.action) params.action = filters.action;
    if (filters.date_from) params.date_from = filters.date_from;
    if (filters.date_to) params.date_to = filters.date_to;
    getAuditLogs(params).then(setLogs).finally(() => setLoading(false));
  };

  useEffect(() => { getAuditActions().then(setActions); }, []);
  useEffect(() => { fetchLogs(); }, [page, filters]);

  const handleExport = async () => {
    const params: Record<string, any> = {};
    if (filters.action) params.action = filters.action;
    if (filters.date_from) params.date_from = filters.date_from;
    if (filters.date_to) params.date_to = filters.date_to;
    const blob = await exportAuditLogs(params);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'audit_logs.csv';
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <Header title="Audit Log" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3">
          <select value={filters.action} onChange={(e) => { setFilters(p => ({ ...p, action: e.target.value })); setPage(0); }}
            className="border border-border rounded-lg px-3 py-2 text-sm bg-card">
            <option value="">All actions</option>
            {actions.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <input type="date" value={filters.date_from} onChange={(e) => { setFilters(p => ({ ...p, date_from: e.target.value })); setPage(0); }}
            className="border border-border rounded-lg px-3 py-2 text-sm" placeholder="From" />
          <input type="date" value={filters.date_to} onChange={(e) => { setFilters(p => ({ ...p, date_to: e.target.value })); setPage(0); }}
            className="border border-border rounded-lg px-3 py-2 text-sm" placeholder="To" />
          <button onClick={fetchLogs} className="flex items-center gap-1 border border-border text-muted-foreground px-3 py-2 rounded-lg text-sm hover:bg-muted/40">
            <RefreshCw size={14} /> Refresh
          </button>
          <button onClick={handleExport} className="flex items-center gap-1 bg-purple-600 text-white px-3 py-2 rounded-lg text-sm font-medium hover:bg-purple-700">
            <Download size={14} /> Export CSV
          </button>
        </div>

        {loading ? <LoadingSpinner /> : (
          <div className="bg-card rounded-lg border border-border overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="bg-muted/40 border-b border-border">
                  <th className="w-8"></th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Timestamp</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">User</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Action</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Resource</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <>
                    <tr key={log.id} onClick={() => setExpandedId(expandedId === log.id ? null : log.id)}
                      className="border-b border-border cursor-pointer hover:bg-muted/40">
                      <td className="px-2 text-muted-foreground">
                        {expandedId === log.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">{formatDateTime(log.created_at)}</td>
                      <td className="px-4 py-3 text-sm text-foreground">{log.user_email || '-'}</td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${ACTION_COLORS[log.action] || 'bg-muted text-muted-foreground'}`}>
                          {log.action}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {log.resource_type && <span className="font-mono">{log.resource_type}:{log.resource_id}</span>}
                      </td>
                    </tr>
                    {expandedId === log.id && (
                      <tr key={`${log.id}-detail`} className="bg-muted/40">
                        <td colSpan={5} className="px-6 py-3">
                          <div className="text-xs space-y-1">
                            <p><strong>User ID:</strong> {log.user_id || '-'}</p>
                            <p><strong>IP Address:</strong> {log.ip_address || '-'}</p>
                            {log.details && (
                              <div>
                                <strong>Details:</strong>
                                <pre className="mt-1 bg-card border border-border rounded p-2 text-xs font-mono overflow-x-auto">
                                  {JSON.stringify(log.details, null, 2)}
                                </pre>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
                {logs.length === 0 && (
                  <tr><td colSpan={5} className="text-center py-12 text-sm text-muted-foreground">No audit logs found</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        <div className="flex justify-between items-center text-sm text-muted-foreground">
          <p>Page {page + 1} ({logs.length} entries)</p>
          <div className="flex gap-2">
            <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
              className="px-3 py-1 border border-border rounded text-sm disabled:opacity-50 hover:bg-muted/40">Prev</button>
            <button onClick={() => setPage(p => p + 1)} disabled={logs.length < PAGE_SIZE}
              className="px-3 py-1 border border-border rounded text-sm disabled:opacity-50 hover:bg-muted/40">Next</button>
          </div>
        </div>
      </div>
    </>
  );
}
