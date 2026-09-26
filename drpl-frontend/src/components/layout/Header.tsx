import { useNavigate } from 'react-router-dom'
import { User, Menu, LogOut, Settings as SettingsIcon } from 'lucide-react'
import { useAuth } from '@/context/AuthContext'
import { useMobileNav } from './AppLayout'
import ThemeToggle from './ThemeToggle'
import NotificationBell from '../notifications/NotificationBell'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

interface HeaderProps {
  title: string
  subtitle?: string
}

/**
 * Per-page top bar. Keeps its original (title, subtitle) API so existing pages
 * render unchanged, but now also carries the global controls: mobile nav
 * trigger, theme toggle, notifications, and the user menu.
 */
export default function Header({ title, subtitle }: HeaderProps) {
  const { auth, logout } = useAuth()
  const { openNav } = useMobileNav()
  const navigate = useNavigate()

  return (
    <header className="sticky top-0 z-20 flex min-h-[72px] items-center justify-between gap-3 border-b border-border bg-card/95 px-4 py-2 backdrop-blur-xl supports-[backdrop-filter]:bg-card/85 sm:px-6 lg:px-8">
      <div className="flex min-w-0 items-center gap-2">
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden"
          aria-label="Open navigation"
          onClick={openNav}
        >
          <Menu className="h-5 w-5" />
        </Button>
        <div className="min-w-0">
          <h1 className="truncate text-base font-bold leading-tight tracking-tight text-foreground sm:text-lg">
            {title}
          </h1>
          {subtitle && (
            <p className="mt-0.5 truncate text-xs text-muted-foreground">{subtitle}</p>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1.5">
        <ThemeToggle />
        <NotificationBell />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              className="h-10 gap-2 px-2"
              aria-label="User menu"
            >
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-700 dark:text-emerald-400">
                <User size={14} strokeWidth={2} />
              </span>
              <span className="hidden max-w-[160px] truncate text-xs font-medium text-muted-foreground sm:inline">
                {auth.email}
              </span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel className="truncate font-normal text-muted-foreground">
              {auth.email}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => navigate('/settings')}>
              <SettingsIcon className="h-4 w-4" />
              Settings
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={logout}
              className="text-destructive focus:text-destructive"
            >
              <LogOut className="h-4 w-4" />
              Logout
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  )
}
