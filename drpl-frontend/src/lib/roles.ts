/**
 * Roles and surfaces — the frontend half of `app/core/roles.py`.
 *
 * This table decides which links a user is shown and which routes render.
 * It is **cosmetic**: `localStorage.drpl_role` is user-editable, so every
 * restriction expressed here is also enforced server-side. Hiding a link is a
 * courtesy so people are not shown doors they cannot open; the lock is the
 * `require_surface` dependency on the backend router.
 *
 * Keep the surface names identical to SURFACES in `app/core/roles.py`.
 */

export const MASTER_ADMIN = 'master_admin'
export const TENDER_SEARCH = 'tender_search'
export const COSTING_RESEARCH = 'costing_research'

export type Role = typeof MASTER_ADMIN | typeof TENDER_SEARCH | typeof COSTING_RESEARCH

export type Surface =
  | 'dashboard' | 'tenders' | 'archive' | 'notifications'
  | 'command_center' | 'workspace' | 'cost_breakdown'
  | 'documents' | 'signatures' | 'document_generator'
  | 'reviews' | 'settings' | 'admin'

export const SURFACES: Surface[] = [
  'dashboard', 'tenders', 'archive', 'notifications',
  'command_center', 'workspace', 'cost_breakdown',
  'documents', 'signatures', 'document_generator',
  'reviews', 'settings', 'admin',
]

/** Old values users hold right now. They must not lose access on deploy. */
const LEGACY: Record<string, Role> = {
  admin: TENDER_SEARCH,
  operator: COSTING_RESEARCH,
  user: COSTING_RESEARCH,
}

/** Unknown or missing resolves to the narrowest role, never the widest. */
export function normalizeRole(role: string | null | undefined): Role {
  if (role === MASTER_ADMIN || role === TENDER_SEARCH || role === COSTING_RESEARCH) return role
  if (role && LEGACY[role]) return LEGACY[role]
  return COSTING_RESEARCH
}

const ALLOWED: Record<Exclude<Role, typeof MASTER_ADMIN>, Surface[]> = {
  [TENDER_SEARCH]: SURFACES.filter((s) => s !== 'admin'),
  [COSTING_RESEARCH]: [
    'dashboard', 'tenders', 'archive', 'notifications',
    'command_center', 'workspace', 'cost_breakdown', 'settings',
  ],
}

export function canAccess(role: string | null | undefined, surface: Surface): boolean {
  const normalized = normalizeRole(role)
  if (normalized === MASTER_ADMIN) return true
  return ALLOWED[normalized].includes(surface)
}

/** Human labels for the user-management screen. */
export const ROLE_LABELS: Record<Role, string> = {
  [MASTER_ADMIN]: 'Master admin',
  [TENDER_SEARCH]: 'Tender search',
  [COSTING_RESEARCH]: 'Costing research',
}

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  [MASTER_ADMIN]: 'Full platform access, including every admin setting and all users’ work.',
  [TENDER_SEARCH]: 'The whole platform except admin settings.',
  [COSTING_RESEARCH]:
    'Costing only: Ask DRPL, tenders, the dashboard, tender workspaces, cost breakdowns, archive and notifications.',
}
