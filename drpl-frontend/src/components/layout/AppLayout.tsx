import { createContext, useContext, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar, { useSidebarCollapsed } from './Sidebar'
import SidebarNav from './SidebarNav'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import GlobalAssistant from '@/components/assistant/GlobalAssistant'

/** Lets per-page Headers open the mobile nav drawer. */
const MobileNavContext = createContext<{ openNav: () => void }>({ openNav: () => {} })
export const useMobileNav = () => useContext(MobileNavContext)

export default function AppLayout() {
  const { collapsed, toggle } = useSidebarCollapsed()
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()
  // /command-center/300 and /tenders/5133/workspace/88 collapse to their
  // route shape, so navigating between records does not remount the page.
  const pageAnimationKey = location.pathname.replace(/\/\d+(?=\/|$)/g, '')

  return (
    <MobileNavContext.Provider value={{ openNav: () => setMobileOpen(true) }}>
      <div className="flex h-screen overflow-hidden bg-background text-foreground">
        <a href="#main-content" className="fixed left-3 top-3 z-[100] -translate-y-20 rounded-xl bg-foreground px-4 py-2 text-sm font-bold text-background shadow-lg transition-transform focus:translate-y-0">Skip to main content</a>
        {/* Desktop rail */}
        <Sidebar collapsed={collapsed} onToggle={toggle} />

        {/* Mobile drawer */}
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetContent side="left" className="w-64 p-0">
            <div className="flex h-16 items-center border-b border-border px-4">
              <img src="/drpl-logo.svg" alt="DRPL" className="h-8 w-auto dark:brightness-0 dark:invert" />
            </div>
            <SidebarNav onNavigate={() => setMobileOpen(false)} />
          </SheetContent>
        </Sheet>

        {/* Content column */}
        <main id="main-content" className="flex h-full min-w-0 flex-1 flex-col overflow-y-auto bg-background">
          {/* The animation key deliberately ignores record ids.
              With the raw pathname here, the Command Center's mid-send URL sync
              (/command-center -> /command-center/300, done right after the
              session is created) changed this key and React unmounted the whole
              routed subtree — destroying the optimistic message, the streaming
              state and the in-flight request. The page reappeared empty while
              the run carried on server-side. Pages already react to their own
              param changes via effects, which is where that belongs. */}
          <div key={pageAnimationKey} className="flex min-h-full flex-1 flex-col animate-page-enter">
            <Outlet />
          </div>
        </main>

        {/* Mounted here, outside the routed <Outlet />, so the assistant and
            its conversation survive navigation instead of remounting per page. */}
        <GlobalAssistant />
      </div>
    </MobileNavContext.Provider>
  )
}
