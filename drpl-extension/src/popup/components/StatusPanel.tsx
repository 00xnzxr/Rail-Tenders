import React, { useEffect, useState } from 'react';

interface ScrapeSession {
  id: string;
  portal: string;
  startedAt: string;
  completedAt: string | null;
  tendersFound: number;
  status: string;
  error?: string;
}

interface StatusData {
  pendingCount: number;
  recentSessions: ScrapeSession[];
  lastSync: string | null;
}

export default function StatusPanel() {
  const [status, setStatus] = useState<StatusData | null>(null);

  useEffect(() => {
    loadStatus();
    const interval = setInterval(loadStatus, 3000);
    return () => clearInterval(interval);
  }, []);

  const loadStatus = () => {
    chrome.runtime.sendMessage({ type: 'GET_SCRAPE_STATUS' }, (response) => {
      if (response && !chrome.runtime.lastError) {
        setStatus(response);
      } else {
        chrome.storage.local.get(
          ['scrapeHistory', 'lastSyncTimestamp', 'pendingUploadCount'],
          (result) => {
            setStatus({
              pendingCount: result.pendingUploadCount || 0,
              recentSessions: (result.scrapeHistory || []).slice(-5),
              lastSync: result.lastSyncTimestamp || null,
            });
          }
        );
      }
    });
  };

  if (!status) {
    return (
      <div className="text-[11px] text-slate-400 text-center py-6 font-medium">
        Loading status…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Quick stats */}
      <div className="grid grid-cols-2 gap-2">
        <StatCard label="Pending Upload" value={status.pendingCount} />
        <StatCard
          label="Last Sync"
          value={status.lastSync ? timeAgo(status.lastSync) : 'Never'}
        />
      </div>

      {/* Recent sessions */}
      <div>
        <h3 className="text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-2">
          Recent Sessions
        </h3>
        {status.recentSessions.length === 0 ? (
          <div className="text-[11px] text-slate-400 text-center py-5 bg-white rounded-lg border border-slate-100">
            No scraping sessions yet
          </div>
        ) : (
          <div className="space-y-1.5">
            {status.recentSessions
              .slice()
              .reverse()
              .map((session) => (
                <SessionRow key={session.id} session={session} />
              ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-white rounded-xl border border-slate-100 p-3 shadow-card">
      <div className="text-xl font-bold text-slate-800 leading-tight">{value}</div>
      <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wide mt-1">{label}</div>
    </div>
  );
}

function SessionRow({ session }: { session: ScrapeSession }) {
  const statusColors: Record<string, string> = {
    running: 'text-blue-600 bg-blue-50',
    uploading: 'text-amber-600 bg-amber-50',
    completed: 'text-emerald-600 bg-emerald-50',
    error: 'text-red-600 bg-red-50',
  };

  const statusLabel: Record<string, string> = {
    running: 'extracting',
    uploading: 'uploading',
    completed: 'synced',
    error: 'error',
  };

  return (
    <div className="bg-white rounded-lg px-3 py-2 border border-slate-100">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-semibold text-slate-700 uppercase tracking-wide truncate">
            {portalLabel(session.portal)}
          </span>
          <span className="text-[10px] text-slate-400 shrink-0">{timeAgo(session.startedAt)}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {session.tendersFound > 0 && (
            <span className="text-[10px] font-medium text-slate-500">
              {session.tendersFound} items
            </span>
          )}
          <span
            className={`text-[10px] font-semibold px-1.5 py-0.5 rounded-md ${
              statusColors[session.status] || 'text-slate-500 bg-slate-100'
            }`}
          >
            {statusLabel[session.status] || session.status}
          </span>
        </div>
      </div>
      {session.error && (
        <p className="text-[10px] text-red-500 mt-1 truncate" title={session.error}>
          {session.error}
        </p>
      )}
    </div>
  );
}

function portalLabel(portal: string): string {
  // Map internal portal keys to neutral, non-branded labels for display.
  const labels: Record<string, string> = {
    ireps: 'Portal A',
    gem: 'Portal B',
    tendertiger: 'Aggregator 1',
    bidassist: 'Aggregator 2',
    tenderdetail: 'Aggregator 3',
    tendersinfo: 'Aggregator 4',
    projectstoday: 'Aggregator 5',
  };
  return labels[portal] || portal;
}

function timeAgo(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
