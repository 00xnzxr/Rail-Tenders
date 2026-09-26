import { useState, useEffect } from 'react';
import { Users, FileText, MessageSquareText, HardDrive, Activity, Shield, Database, Bot, Clock } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getAdminDashboard, getAdminHealth, getAdminApiUsage, getAdminActiveUsers, getAuditLogs } from '../../lib/api';
import { formatDateTime } from '../../lib/formatters';

interface HealthCheck {
  status: string;
  message: string;
}

const HEALTH_COLORS: Record<string, string> = {
  healthy: 'bg-emerald-500',
  warning: 'bg-amber-500',
  error: 'bg-red-500',
  degraded: 'bg-amber-500',
};

function StatCard({ icon: Icon, label, value, sub, color }: { icon: any; label: string; value: string | number; sub?: string; color: string }) {
  return (
    <div className="bg-card rounded-lg border border-border p-5">
      <div className="flex items-center gap-3">
        <div className={`w-10 h-10 rounded-lg ${color} flex items-center justify-center`}>
          <Icon size={18} className="text-white" />
        </div>
        <div>
          <p className="text-2xl font-bold text-foreground">{value}</p>
          <p className="text-xs text-muted-foreground">{label}</p>
          {sub && <p className="text-[10px] text-muted-foreground mt-0.5">{sub}</p>}
        </div>
      </div>
    </div>
  );
}

export default function AdminDashboardPage() {
  const [overview, setOverview] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [apiUsage, setApiUsage] = useState<any>(null);
  const [activeUsers, setActiveUsers] = useState<any[]>([]);
  const [recentLogs, setRecentLogs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      getAdminDashboard().catch(() => null),
      getAdminHealth().catch(() => null),
      getAdminApiUsage(30).catch(() => null),
      getAdminActiveUsers().catch(() => []),
      getAuditLogs({ limit: 10 }).catch(() => []),
    ]).then(([ov, hl, usage, users, logs]) => {
      setOverview(ov);
      setHealth(hl);
      setApiUsage(usage);
      setActiveUsers(users);
      setRecentLogs(logs);
    }).finally(() => setLoading(false));
  }, []);

  if (loading) return <><Header title="Admin Dashboard" /><LoadingSpinner /></>;

  const ROLE_COLORS: Record<string, string> = {
    master_admin: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
    admin: 'bg-accent/15 text-accent',
    operator: 'bg-muted text-muted-foreground',
  };

  return (
    <>
      <Header title="Admin Dashboard" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Stat cards */}
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
          <StatCard icon={Users} label="Total Users" value={overview?.users?.total || 0} sub={`${overview?.users?.active_7d || 0} active (7d)`} color="bg-accent" />
          <StatCard icon={FileText} label="Total Tenders" value={overview?.tenders?.total || 0} sub={`${overview?.tenders?.open || 0} open`} color="bg-emerald-500" />
          <StatCard icon={MessageSquareText} label="Proposals" value={overview?.proposals?.total || 0} sub={`${overview?.proposals?.by_status?.approved || 0} approved`} color="bg-purple-500" />
          <StatCard icon={HardDrive} label="Storage" value={`${overview?.storage_mb || 0} MB`} color="bg-amber-500" />
          <StatCard icon={Activity} label="AI Calls (30d)" value={apiUsage?.total_calls || 0} sub={`${apiUsage?.error_count || 0} errors`} color="bg-indigo-500" />
        </div>

        {/* System Health + API Usage */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* System Health */}
          <div className="bg-card rounded-lg border border-border p-5">
            <h3 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2"><Shield size={16} /> System Health</h3>
            {health?.checks ? (
              <div className="space-y-3">
                {Object.entries(health.checks as Record<string, HealthCheck>).map(([name, check]) => (
                  <div key={name} className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className={`w-3 h-3 rounded-full ${HEALTH_COLORS[check.status] || 'bg-muted-foreground/40'}`} />
                      <div>
                        <p className="text-sm font-medium text-foreground capitalize">{name.replace('_', ' ')}</p>
                        <p className="text-xs text-muted-foreground">{check.message}</p>
                      </div>
                    </div>
                    <span className={`text-xs font-medium px-2 py-0.5 rounded ${
                      check.status === 'healthy' ? 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400' :
                      check.status === 'warning' ? 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400' : 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400'
                    }`}>
                      {check.status}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Health check unavailable</p>
            )}
          </div>

          {/* API Usage Summary */}
          <div className="bg-card rounded-lg border border-border p-5">
            <h3 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2"><Bot size={16} /> AI API Usage (30 days)</h3>
            {apiUsage ? (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-4">
                  <div className="bg-muted/40 rounded-lg p-3">
                    <p className="text-xs text-muted-foreground">Total Calls</p>
                    <p className="text-xl font-bold text-foreground">{apiUsage.total_calls}</p>
                  </div>
                  <div className="bg-muted/40 rounded-lg p-3">
                    <p className="text-xs text-muted-foreground">Est. Cost</p>
                    <p className="text-xl font-bold text-foreground">${apiUsage.total_cost_estimate?.toFixed(4) || '0.00'}</p>
                  </div>
                  <div className="bg-muted/40 rounded-lg p-3">
                    <p className="text-xs text-muted-foreground">Input Tokens</p>
                    <p className="text-lg font-semibold text-foreground">{(apiUsage.total_tokens_input || 0).toLocaleString()}</p>
                  </div>
                  <div className="bg-muted/40 rounded-lg p-3">
                    <p className="text-xs text-muted-foreground">Output Tokens</p>
                    <p className="text-lg font-semibold text-foreground">{(apiUsage.total_tokens_output || 0).toLocaleString()}</p>
                  </div>
                </div>
                {apiUsage.error_count > 0 && (
                  <div className="bg-red-50 dark:bg-red-500/15 border border-red-100 rounded-lg p-2 text-xs text-red-600 dark:text-red-400">
                    {apiUsage.error_count} errors ({(apiUsage.error_rate * 100).toFixed(1)}% error rate)
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No usage data available</p>
            )}
          </div>
        </div>

        {/* Active Users + Recent Activity */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Active Users */}
          <div className="bg-card rounded-lg border border-border p-5">
            <h3 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2"><Users size={16} /> Active Users (7 days)</h3>
            {activeUsers.length > 0 ? (
              <div className="space-y-2">
                {activeUsers.map((user: any) => (
                  <div key={user.id} className="flex items-center justify-between py-1.5">
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-full bg-muted flex items-center justify-center text-xs font-medium text-muted-foreground">
                        {user.name?.charAt(0)?.toUpperCase() || '?'}
                      </div>
                      <div>
                        <p className="text-sm font-medium text-foreground">{user.name}</p>
                        <p className="text-xs text-muted-foreground">{user.email}</p>
                      </div>
                    </div>
                    <div className="text-right">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${ROLE_COLORS[user.role] || ROLE_COLORS.operator}`}>{user.role}</span>
                      <p className="text-[10px] text-muted-foreground mt-0.5">{formatDateTime(user.last_login_at)}</p>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No recent activity</p>
            )}
          </div>

          {/* Recent Activity Feed */}
          <div className="bg-card rounded-lg border border-border p-5">
            <h3 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2"><Clock size={16} /> Recent Activity</h3>
            {recentLogs.length > 0 ? (
              <div className="space-y-2">
                {recentLogs.map((log: any) => (
                  <div key={log.id} className="flex items-start gap-3 py-1.5">
                    <div className="w-2 h-2 rounded-full bg-purple-400 mt-1.5 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-foreground">
                        <span className="font-medium">{log.user_email || 'System'}</span>
                        {' '}
                        <span className="text-muted-foreground">{log.action.replace('.', ' → ')}</span>
                        {log.resource_type && (
                          <span className="text-muted-foreground text-xs"> ({log.resource_type}:{log.resource_id})</span>
                        )}
                      </p>
                      <p className="text-[10px] text-muted-foreground">{formatDateTime(log.created_at)}</p>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No activity yet. Actions like logins, user changes, and settings updates will appear here.</p>
            )}
          </div>
        </div>

        {/* Users by Role */}
        {overview?.users?.by_role && (
          <div className="bg-card rounded-lg border border-border p-5">
            <h3 className="text-sm font-semibold text-foreground mb-3">Users by Role</h3>
            <div className="flex gap-4">
              {Object.entries(overview.users.by_role as Record<string, number>).map(([role, count]) => (
                <div key={role} className="flex items-center gap-2">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${ROLE_COLORS[role] || ROLE_COLORS.operator}`}>{role}</span>
                  <span className="text-sm font-semibold text-foreground">{count}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </>
  );
}
