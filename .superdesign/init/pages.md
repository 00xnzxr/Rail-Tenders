# Key page dependency trees

## `/` Home

- `src/pages/DashboardPage.tsx`
  - `components/layout/Header.tsx`
  - `components/dashboard/HeroSummary.tsx`
  - `components/dashboard/MeaningfulStats.tsx`
  - `components/dashboard/ActionQueue.tsx`
  - `components/dashboard/LatestCostings.tsx`
  - `components/dashboard/ScoringStrip.tsx`
  - `components/dashboard/RecentTendersTable.tsx`
  - `hooks/useStats.ts`, `hooks/useTenders.ts`

## `/tenders`

- `src/pages/TendersPage.tsx`
  - `components/tenders/SegmentFunnel.tsx`
  - `components/tenders/FilterBar.tsx`
  - `components/tenders/TenderCardList.tsx`
    - `components/tenders/TenderCard.tsx`
  - `components/ui/Pagination.tsx`
  - `hooks/useTenders.ts`

## `/command-center`

- `src/pages/CommandCenterPage.tsx`
  - session sidebar/filter/switcher components
  - chat MarkdownMessage and suggestion components
  - agent status, plan, timeline, clarification, artifact, workspace, and document panels
  - `lib/api.ts` streaming/session APIs

## Shared shell

- `components/layout/AppLayout.tsx`
  - `components/layout/Sidebar.tsx`
    - `components/layout/SidebarNav.tsx`
  - `components/layout/Header.tsx`
  - theme, notification, dropdown, sheet, button primitives

