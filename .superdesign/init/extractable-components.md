# Extractable components

## AppSidebar
- Source: `drpl-frontend/src/components/layout/Sidebar.tsx` and `SidebarNav.tsx`
- Category: layout
- Description: persistent responsive navigation with role-aware administration.
- Props: collapsed, activeItem, adminOpen.

## GlobalHeader
- Source: `drpl-frontend/src/components/layout/Header.tsx`
- Category: layout
- Description: page title/greeting, theme, notifications, and user menu.
- Props: title, subtitle, showGreeting.

## PriorityCard
- Source anchor: `drpl-frontend/src/components/dashboard/ActionQueue.tsx`
- Category: basic
- Description: plain-language priority count and direct destination.
- Props: title, count, urgency, actionLabel.

## TenderCard
- Source: `drpl-frontend/src/components/tenders/TenderCard.tsx`
- Category: basic
- Description: tender summary with fit, deadline, organization, status, and details.
- Props: title, organization, score, dueDate, status, searchQuery.

## AskDrplPanel
- Source anchors: `drpl-frontend/src/pages/CommandCenterPage.tsx` and `components/command-center/SuggestionBar.tsx`
- Category: basic
- Description: contextual AI entry point with familiar task starters.
- Props: tenderId, prompt, suggestions.
