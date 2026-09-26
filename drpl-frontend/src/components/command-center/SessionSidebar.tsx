import { useMemo, useState } from 'react';
import {
  ChevronLeft,
  ChevronRight,
  FileText,
  MessageSquare,
  Plus,
  Search,
  Trash2,
} from 'lucide-react';
import type { CommandCenterSession } from '../../types/command-center';
import { useSidebarCollapsed } from '../../hooks/useSidebarCollapsed';
import type { SessionFilter, SessionSortKind } from './applySessionFilters';
import SessionFilterBar from './SessionFilterBar';

interface SessionSidebarProps {
  sessions: CommandCenterSession[];
  currentSessionId: number | null;
  onSwitch: (sessionId: number) => void;
  onNew: () => void;
  onDelete?: (sessionId: number) => void;
  isLoading?: boolean;
  filter?: SessionFilter;
  onFilterChange?: (next: SessionFilter) => void;
  sort?: SessionSortKind;
  onSortChange?: (next: SessionSortKind) => void;
  activeFilterCount?: number;
  onClearFilters?: () => void;
}

type SessionGroup = { label: string; sessions: CommandCenterSession[] };

function cleanSessionTitle(session: CommandCenterSession): string {
  const raw = (session.tender_title || session.title || 'Untitled chat').trim();
  return raw
    .replace(/^command\s*center\s*/i, '')
    .replace(/\s*\(\d+\)\s*$/, '')
    .trim() || 'Untitled chat';
}

function groupSessions(sessions: CommandCenterSession[]): SessionGroup[] {
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const sevenDaysAgo = startToday - 6 * 24 * 60 * 60 * 1000;
  const groups: Record<'Today' | 'Previous 7 days' | 'Older', CommandCenterSession[]> = {
    Today: [],
    'Previous 7 days': [],
    Older: [],
  };
  for (const session of sessions) {
    const stamp = new Date(session.updated_at || session.created_at).getTime();
    if (Number.isFinite(stamp) && stamp >= startToday) groups.Today.push(session);
    else if (Number.isFinite(stamp) && stamp >= sevenDaysAgo) groups['Previous 7 days'].push(session);
    else groups.Older.push(session);
  }
  return (Object.entries(groups) as Array<[SessionGroup['label'], CommandCenterSession[]]>)
    .filter(([, items]) => items.length > 0)
    .map(([label, items]) => ({ label, sessions: items }));
}

function formatActivity(session: CommandCenterSession): string {
  const date = new Date(session.updated_at || session.created_at);
  if (Number.isNaN(date.getTime())) return session.tender_id ? 'Tender chat' : 'General chat';
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
  return date.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

export default function SessionSidebar({
  sessions,
  currentSessionId,
  onSwitch,
  onNew,
  onDelete,
  isLoading,
  filter,
  onFilterChange,
  sort,
  onSortChange,
  activeFilterCount,
  onClearFilters,
}: SessionSidebarProps) {
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const [query, setQuery] = useState('');
  const visibleSessions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return sessions;
    return sessions.filter((session) => cleanSessionTitle(session).toLowerCase().includes(normalized));
  }, [query, sessions]);
  const grouped = useMemo(() => groupSessions(visibleSessions), [visibleSessions]);

  const showFilters = !collapsed && filter !== undefined && onFilterChange && sort !== undefined
    && onSortChange && activeFilterCount !== undefined && onClearFilters;

  return (
    <nav
      role="navigation"
      aria-label="Chat history"
      style={{ width: collapsed ? '3.75rem' : '18rem' }}
      className="hidden flex-shrink-0 flex-col overflow-hidden border-r border-border bg-card transition-[width] duration-200 sm:flex"
    >
      <div className="border-b border-border p-3">
        <div className="flex items-center justify-between gap-2">
          {!collapsed && <h2 className="text-sm font-bold text-foreground">Chats</h2>}
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="ml-auto flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            title={collapsed ? 'Expand chat history' : 'Collapse chat history'}
            aria-label={collapsed ? 'Expand chat history' : 'Collapse chat history'}
          >
            {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
          </button>
        </div>
        <button
          onClick={onNew}
          className={`mt-3 flex h-10 w-full items-center gap-2 rounded-xl border border-border bg-background px-3 text-sm font-semibold text-foreground shadow-sm transition-colors hover:border-emerald-500/40 hover:bg-emerald-500/5 ${collapsed ? 'justify-center px-0' : ''}`}
          title={collapsed ? 'New chat' : undefined}
        >
          <Plus size={16} className="text-emerald-600" />
          {!collapsed && <span>New chat</span>}
        </button>
        {!collapsed && (
          <label className="mt-2.5 flex h-9 items-center gap-2 rounded-xl border border-border bg-muted/30 px-3 focus-within:border-emerald-500/40 focus-within:bg-background">
            <Search size={14} className="shrink-0 text-muted-foreground" />
            <span className="sr-only">Search chats</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search chats"
              className="min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none placeholder:text-muted-foreground"
            />
          </label>
        )}
      </div>

      {showFilters && (
        <SessionFilterBar
          filter={filter}
          onFilterChange={onFilterChange}
          sort={sort}
          onSortChange={onSortChange}
          activeFilterCount={activeFilterCount}
          onClearFilters={onClearFilters}
          compact
        />
      )}

      <div className="flex-1 overflow-y-auto px-2 py-2">
        {isLoading && sessions.length === 0 ? (
          <SkeletonRows collapsed={collapsed} />
        ) : visibleSessions.length === 0 ? (
          <EmptyState query={query} filtered={(activeFilterCount ?? 0) > 0} onClearFilters={onClearFilters} />
        ) : collapsed ? (
          visibleSessions.map((session) => (
            <SessionRow key={session.id} session={session} isActive={session.id === currentSessionId} collapsed onSwitch={() => onSwitch(session.id)} />
          ))
        ) : (
          grouped.map((group) => (
            <section key={group.label} className="mb-4">
              <p className="px-2 pb-1.5 pt-1 text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground/80">{group.label}</p>
              <div className="space-y-0.5">
                {group.sessions.map((session) => (
                  <SessionRow
                    key={session.id}
                    session={session}
                    isActive={session.id === currentSessionId}
                    collapsed={false}
                    onSwitch={() => onSwitch(session.id)}
                    onDelete={onDelete ? () => onDelete(session.id) : undefined}
                  />
                ))}
              </div>
            </section>
          ))
        )}
      </div>
    </nav>
  );
}

function SessionRow({ session, isActive, collapsed, onSwitch, onDelete }: {
  session: CommandCenterSession;
  isActive: boolean;
  collapsed: boolean;
  onSwitch: () => void;
  onDelete?: () => void;
}) {
  const isTender = !!session.tender_id;
  const title = cleanSessionTitle(session);
  const Icon = isTender ? FileText : MessageSquare;
  return (
    <div className={`group relative rounded-xl transition-colors ${isActive ? 'bg-emerald-500/10' : 'hover:bg-muted/60'}`}>
      <button onClick={onSwitch} className={`flex w-full items-start gap-2.5 rounded-xl px-2 py-2.5 text-left ${collapsed ? 'justify-center px-0' : ''}`} title={collapsed ? title : undefined}>
        <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${isActive ? 'bg-emerald-600 text-white' : 'bg-muted text-muted-foreground'}`}><Icon size={13} /></span>
        {!collapsed && (
          <span className="min-w-0 flex-1 pr-5">
            <span className={`block truncate text-[13px] leading-5 ${isActive ? 'font-semibold text-foreground' : 'font-medium text-foreground'}`}>{title}</span>
            <span className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground"><span>{isTender ? 'Tender' : 'General'}</span><span aria-hidden>·</span><span>{formatActivity(session)}</span>{(session.message_count ?? 0) > 0 && <><span aria-hidden>·</span><span>{session.message_count} msgs</span></>}</span>
          </span>
        )}
      </button>
      {!collapsed && onDelete && (
        <button onClick={(event) => { event.stopPropagation(); onDelete(); }} className="absolute right-2 top-2.5 flex h-7 w-7 items-center justify-center rounded-lg bg-card text-muted-foreground opacity-0 shadow-sm transition-opacity hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100 group-focus-within:opacity-100" title="Delete chat" aria-label={`Delete ${title}`}><Trash2 size={13} /></button>
      )}
    </div>
  );
}

function SkeletonRows({ collapsed }: { collapsed: boolean }) {
  return <div className="space-y-2">{[0, 1, 2, 3].map((index) => <div key={index} className={`flex animate-pulse items-center gap-2 px-2 py-2 ${collapsed ? 'justify-center' : ''}`}><div className="h-7 w-7 rounded-lg bg-muted" />{!collapsed && <div className="space-y-1.5 flex-1"><div className="h-3 rounded bg-muted" /><div className="h-2 w-1/2 rounded bg-muted" /></div>}</div>)}</div>;
}

function EmptyState({ query, filtered, onClearFilters }: { query: string; filtered: boolean; onClearFilters?: () => void }) {
  return (
    <div className="px-4 py-10 text-center">
      <MessageSquare size={22} className="mx-auto mb-2 text-muted-foreground/40" />
      <p className="text-xs font-medium text-foreground">{query ? 'No matching chats' : filtered ? 'No chats match these filters' : 'No chats yet'}</p>
      {filtered && onClearFilters && <button onClick={onClearFilters} className="mt-2 text-xs font-medium text-emerald-700 hover:underline dark:text-emerald-400">Clear filters</button>}
    </div>
  );
}
