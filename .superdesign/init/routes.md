# Route map

Router source: `drpl-frontend/src/router.tsx`

## Everyday routes

- `/` → role-dependent HomeLanding (currently Command Center for ordinary users and Dashboard for admins; redesign requirement is Home for everyone)
- `/tenders` and `/tenders/view/:view` → TendersPage
- `/tenders/:id` → TenderDetailPage
- `/tenders/:id/checklist` → ChecklistPage
- `/tenders/:id/command-center` → contextual CommandCenterPage
- `/command-center` and `/command-center/:sessionId` → CommandCenterPage
- `/documents`, `/documents/sign/:id`, `/documents/generate/:id` → document/signing flows
- `/notifications`, `/settings`, `/archive`, `/scrape-monitor`

## Administration

Role-gated routes cover reviews, users, settings, tender scope/scoring, security/audit, templates, letterheads, agent/workflow configuration, pipelines, memory, datasets, ratecards, MCP servers, batch processing, and context management.

The redesign must preserve route capability while simplifying the everyday information architecture and isolating administrative tools.

