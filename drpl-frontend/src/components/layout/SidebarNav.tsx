import { NavLink } from 'react-router-dom'
import { useState } from 'react'
import {
  Home, FileText, Activity, Settings, LogOut,
  ShieldCheck, Users, Sliders, Shield, ClipboardList, Gauge,
  MessageSquare, Files, Layout,
  Cpu, Library, Workflow, Brain, Server, Layers, SlidersHorizontal, Database,
  GitBranch, Target, Bell, ChevronDown, FileSpreadsheet, Briefcase, ScanSearch,
  type LucideIcon,
} from 'lucide-react'
import UsageBar from './UsageBar'
import { useAuth } from '@/context/AuthContext'
import { canAccess, type Surface } from '@/lib/roles'
import { cn } from '@/lib/utils'

// `surface` filters the link out for roles that cannot reach the page — a
// costing researcher is not shown "My Work" only to be bounced off it. The
// server-side gate is the actual control (see src/lib/roles.ts).
const mainNavTop = [
  { to: '/', icon: Home, label: 'Home', end: true, surface: 'dashboard' as Surface },
  { to: '/tenders', icon: FileText, label: 'Tenders', surface: 'tenders' as Surface },
  { to: '/gem-search', icon: ScanSearch, label: 'GeM Search', surface: 'tenders' as Surface },
  { to: '/documents', icon: Briefcase, label: 'My Work', surface: 'documents' as Surface },
  { to: '/command-center', icon: MessageSquare, label: 'Ask DRPL', surface: 'command_center' as Surface },
  { to: '/notifications', icon: Bell, label: 'Notifications', surface: 'notifications' as Surface },
]
const mainNavBottom = [
  { to: '/settings', icon: Settings, label: 'Settings' },
]

const adminNav = [
  { to: '/reviews', icon: ShieldCheck, label: 'Reviews' },
]

const masterAdminGroups = [
  {
    label: 'Platform',
    items: [
      { to: '/admin', icon: Gauge, label: 'Overview', end: true },
      { to: '/admin/users', icon: Users, label: 'People & access' },
      { to: '/admin/usage', icon: Gauge, label: 'Usage & budgets' },
      { to: '/admin/settings', icon: Sliders, label: 'Platform settings' },
      { to: '/admin/security', icon: Shield, label: 'Data security' },
      { to: '/admin/audit', icon: ClipboardList, label: 'Audit history' },
    ],
  },
  {
    label: 'Tender setup',
    items: [
      { to: '/admin/tender-scope', icon: Target, label: 'Tender scope' },
      { to: '/admin/tender-scoring', icon: Activity, label: 'Tender scoring' },
      { to: '/admin/templates', icon: Files, label: 'Templates' },
      { to: '/admin/letterheads', icon: Layout, label: 'Letterheads' },
      { to: '/admin/ratecards', icon: FileSpreadsheet, label: 'Ratecards' },
    ],
  },
  {
    label: 'Automation',
    items: [
      { to: '/admin/agent-builder', icon: Cpu, label: 'Assistant builder' },
      { to: '/admin/agent-library', icon: Library, label: 'Assistant library' },
      { to: '/admin/workflows', icon: GitBranch, label: 'Workflow builder' },
      { to: '/admin/agent-pipeline', icon: Workflow, label: 'Processing flow' },
      { to: '/admin/agent-memory', icon: Brain, label: 'Assistant memory' },
      { to: '/admin/training-datasets', icon: Database, label: 'Training data' },
      { to: '/admin/mcp-servers', icon: Server, label: 'Connections' },
      { to: '/admin/batch-processing', icon: Layers, label: 'Batch processing' },
      { to: '/admin/context-management', icon: SlidersHorizontal, label: 'Context settings' },
    ],
  },
]

const masterAdminNav = masterAdminGroups.flatMap((group) => group.items)

function NavItem({
  to, icon: Icon, label, end, collapsed, onNavigate,
}: {
  to: string; icon: LucideIcon; label: string; end?: boolean; collapsed?: boolean; onNavigate?: () => void
}) {
  return (
    <NavLink
      to={to}
      end={end}
      onClick={onNavigate}
      title={collapsed ? label : undefined}
      className={({ isActive }) =>
        cn(
          'flex min-h-11 items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold transition-all duration-200',
          collapsed && 'justify-center px-0',
          isActive
            ? 'bg-emerald-500/10 text-emerald-700 shadow-sm dark:text-emerald-400'
            : 'text-muted-foreground hover:translate-x-0.5 hover:bg-muted/70 hover:text-foreground'
        )
      }
    >
      <Icon size={18} strokeWidth={1.75} className="shrink-0" />
      {!collapsed && <span className="truncate">{label}</span>}
    </NavLink>
  )
}

/**
 * Shared nav content used by both the desktop sidebar rail and the mobile
 * Sheet drawer. `collapsed` renders the icon-only rail; `onNavigate` lets the
 * mobile drawer close itself after a link is tapped.
 */
export default function SidebarNav({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean
  onNavigate?: () => void
}) {
  const { auth, logout, isAdminOrAbove, isMasterAdmin } = useAuth()
  const visibleTop = mainNavTop.filter((item) => canAccess(auth.role, item.surface))
  // Admin block is collapsed by default so everyday users aren't faced with
  // 16+ platform links. Auto-expanded only when the user opens it.
  const [adminOpen, setAdminOpen] = useState(false)

  return (
    <div className="flex h-full flex-col">
      <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
        {visibleTop.map(({ surface: _s, ...item }) => (
          <NavItem key={item.to} {...item} collapsed={collapsed} onNavigate={onNavigate} />
        ))}

        {isAdminOrAbove && !isMasterAdmin && (
          <div className="pt-3">
            {adminNav.map((item) => (
              <NavItem key={item.to} {...item} collapsed={collapsed} onNavigate={onNavigate} />
            ))}
          </div>
        )}

        {isMasterAdmin && (
          <div className="pt-3">
            {adminNav.map((item) => (
              <NavItem key={item.to} {...item} collapsed={collapsed} onNavigate={onNavigate} />
            ))}
            {collapsed ? (
              masterAdminNav.map((item) => (
                <NavItem key={item.to} {...item} collapsed onNavigate={onNavigate} />
              ))
            ) : (
              <>
                <button
                  type="button"
                  onClick={() => setAdminOpen((v) => !v)}
                  aria-expanded={adminOpen}
                  className="mt-1 flex w-full items-center justify-between rounded-md px-3 py-2 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  Platform controls
                  <ChevronDown
                    size={14}
                    className={cn('transition-transform', adminOpen && 'rotate-180')}
                  />
                </button>
                {adminOpen && masterAdminGroups.map((group) => (
                  <div key={group.label} className="mb-3">
                    <p className="px-3 pb-1 pt-2 text-[10px] font-extrabold uppercase tracking-[0.14em] text-muted-foreground/70">{group.label}</p>
                    {group.items.map((item) => (
                      <NavItem key={item.to} {...item} onNavigate={onNavigate} />
                    ))}
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </nav>

      <div className="space-y-1 border-t border-border px-3 py-3">
        <UsageBar collapsed={collapsed} />
        {mainNavBottom.map((item) => (
          <NavItem key={item.to} {...item} collapsed={collapsed} onNavigate={onNavigate} />
        ))}
        <button
          onClick={logout}
          title={collapsed ? 'Logout' : undefined}
          className={cn(
            'flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive',
            collapsed && 'justify-center px-0'
          )}
        >
          <LogOut size={18} strokeWidth={1.75} className="shrink-0" />
          {!collapsed && 'Logout'}
        </button>
      </div>
    </div>
  )
}
