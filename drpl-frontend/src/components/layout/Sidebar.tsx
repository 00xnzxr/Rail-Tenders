import { useState, useEffect, useCallback } from 'react'
import { PanelLeftClose, PanelLeft } from 'lucide-react'
import SidebarNav from './SidebarNav'
import { cn } from '@/lib/utils'

const STORAGE_KEY = 'drpl_sidebar_collapsed'

export function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem(STORAGE_KEY) === '1'
  )
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0')
  }, [collapsed])
  const toggle = useCallback(() => setCollapsed((v) => !v), [])
  return { collapsed, toggle }
}

/**
 * Desktop sidebar rail. Hidden below md (mobile uses the Sheet drawer in
 * AppLayout). Collapses to an icon-only rail; state persists.
 */
export default function Sidebar({
  collapsed,
  onToggle,
}: {
  collapsed: boolean
  onToggle: () => void
}) {
  return (
    <aside
      className={cn(
        'hidden shrink-0 flex-col border-r border-border bg-card md:flex transition-[width] duration-200 ease-out',
        collapsed ? 'w-[72px]' : 'w-64'
      )}
    >
      <div
        className={cn(
          'flex h-[72px] items-center border-b border-border px-4',
          collapsed && 'justify-center px-0'
        )}
      >
        {!collapsed && (
          <img src="/drpl-logo.svg" alt="DRPL" className="h-8 w-auto dark:brightness-0 dark:invert" />
        )}
        <button
          onClick={onToggle}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className={cn(
            'rounded-xl p-2 text-muted-foreground hover:bg-muted hover:text-foreground',
            !collapsed && 'ml-auto'
          )}
        >
          {collapsed ? <PanelLeft size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>
      <SidebarNav collapsed={collapsed} />
    </aside>
  )
}
