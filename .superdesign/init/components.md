# Shared UI components

Framework: React 18 + TypeScript. Styling: Tailwind CSS with shadcn/Radix primitives.

## Core primitives

- `drpl-frontend/src/components/ui/button.tsx` — variant-based Button using class-variance-authority and Radix Slot.
- `drpl-frontend/src/components/ui/card.tsx` — Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter.
- `drpl-frontend/src/components/ui/input.tsx` — styled native input.
- `drpl-frontend/src/components/ui/badge.tsx` — semantic badge variants.
- `drpl-frontend/src/components/ui/dialog.tsx`, `sheet.tsx`, `dropdown-menu.tsx`, `tabs.tsx`, `tooltip.tsx` — Radix-backed overlays and navigation primitives.

The source implementations are compact wrappers around the project tokens in `src/index.css`. Designs should reuse these primitives and their existing rounded border/focus conventions.

## Shared product components

- `components/ui/EmptyState.tsx` — empty-list guidance.
- `components/ui/LoadingSpinner.tsx` — current loading state; redesign should prefer layout-preserving skeletons.
- `components/ui/Pagination.tsx` — list paging.
- `components/ui/StatusBadge.tsx`, `PriorityBadge.tsx`, `WorkflowBadge.tsx`, `PortalBadge.tsx` — semantic status vocabulary.
- `components/notifications/NotificationBell.tsx` — global notification control.

