# Shared layouts

## App shell

Source: `drpl-frontend/src/components/layout/AppLayout.tsx`

The application is a full-height flex shell. Desktop uses a persistent collapsible sidebar; mobile uses a left Sheet drawer. The content column owns vertical scrolling and renders routes through React Router's `Outlet`.

## Sidebar

Sources:

- `drpl-frontend/src/components/layout/Sidebar.tsx`
- `drpl-frontend/src/components/layout/SidebarNav.tsx`

The sidebar is 240px expanded and 64px collapsed, with the DRPL logo, icon-and-label navigation, a separated admin section, and logout at the bottom. Current top-level navigation exposes Dashboard, Command Center, Tenders, Archive, Documents, Signatures, Scrape Monitor, Notifications, and Settings. The redesign consolidates this into Home, Tenders, My Work, Ask DRPL, and Notifications, with Settings/Help separated and administration role-gated.

## Header

Source: `drpl-frontend/src/components/layout/Header.tsx`

Sticky 64px page header containing title/subtitle, mobile menu, theme toggle, notifications, and user menu. The redesign keeps global controls but gives Home a greeting/daily-briefing header rather than repeating a generic page title.

