import { useLocation, useParams } from 'react-router-dom'
import { useMemo } from 'react'

/**
 * What the assistant knows about where the user is standing.
 *
 * Sent with every popup message so the agent can resolve vague references —
 * "why is this wrong?", "fix this", "what does this mean?" — without the user
 * having to name the tender or record they are looking at.
 */
export interface PageContext {
  route: string
  tender_id?: number
  workspace_tab?: string
  visible_entity?: { type: string; id?: number | string }
  last_error?: string
}

/** Pull a numeric id out of a path like /tenders/412/workspace/checklist. */
function tenderIdFromPath(pathname: string): number | undefined {
  const match = pathname.match(/\/tenders\/(\d+)/)
  if (!match) return undefined
  const id = Number(match[1])
  return Number.isFinite(id) ? id : undefined
}

/** The trailing segment of a workspace route, e.g. 'checklist'. */
function workspaceTabFromPath(pathname: string): string | undefined {
  const match = pathname.match(/\/workspace\/([a-z0-9-]+)/i)
  return match ? match[1] : undefined
}

export function usePageContext(extra?: Partial<PageContext>): PageContext {
  const location = useLocation()
  const params = useParams()

  // `extra` is spread last so a page can name the specific record in focus,
  // which the URL alone cannot tell us.
  return useMemo(() => {
    const routeTenderId =
      tenderIdFromPath(location.pathname) ??
      (params.id ? Number(params.id) : undefined)

    return {
      route: location.pathname,
      ...(Number.isFinite(routeTenderId as number)
        ? { tender_id: routeTenderId as number }
        : {}),
      ...(workspaceTabFromPath(location.pathname)
        ? { workspace_tab: workspaceTabFromPath(location.pathname) }
        : {}),
      ...extra,
    }
  }, [location.pathname, params.id, extra])
}
