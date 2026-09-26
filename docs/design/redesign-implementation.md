# Compact tender desk — implementation coverage

## Delivered direction

The selected compact tender-desk design is the shared product language for the full application. Home is the universal landing page. Everyday work is organized around five destinations: Home, Tenders, My Work, Ask DRPL, and Notifications. Settings is separated at the bottom and platform controls remain role-gated.

## Shared foundation

- Responsive 72px application header and collapsible desktop/mobile navigation.
- Cool neutral canvas, elevated white cards, thin borders, generous rounding, and restrained shadows.
- Emerald for recommended actions and success; blue for navigation and supporting information; amber/red for caution.
- Consistent button, input, textarea, tab, card, dialog, focus, active, page-entry, and reduced-motion behavior.
- Keyboard skip link, visible focus treatment, mobile-visible essential actions, and responsive page gutters.

## Route coverage

### Everyday work

- `/` — live daily tender desk with priorities, recommendation, continue-working, work queue, and Ask DRPL starters.
- `/tenders` and saved tender views — decision funnel, live multi-keyword title search, highlighted matches, filters, responsive tender cards, and advanced bulk tools.
- `/tenders/:id` — task-led tender overview with DRPL review, source information, preparation actions, and contextual assistance.
- Checklist and document workspaces — plain-language preparation labels, document progress, DRPL helper, templates, annexures, review, and export.
- `/documents` and related document/signing routes — unified My Work framing, responsive upload/search/actions, visible mobile controls, and guided empty states.
- `/command-center` — conversation history, focused chat, preparation, documents, and generated Results in a responsive three-part workspace.
- `/notifications` — attention-led list, unread filter, and an actionable caught-up state.
- `/settings` — profile and security first; technical connections and developer controls use progressive disclosure.
- `/login` — simple split-screen entry with a focused sign-in card and a short explanation of DRPL's value.

### Administration

- Platform controls are grouped into Platform, Tender setup, and Automation.
- All admin routes inherit the responsive frame, semantic controls, card system, focus treatment, and motion rules.
- Agent chat and scraper monitoring are restricted to master administrators.

## Content rules now enforced

- Routine screens say Ask DRPL, Results, Preparation, Review, and Checklist instead of exposing agents, artifacts, pipelines, or initialization details.
- Empty and error states explain what happened and what the user can do next.
- Destructive actions retain explicit confirmation.
- Search uses every entered title keyword case-insensitively and visually highlights matched words.

## Verification

- Frontend production TypeScript/Vite build passes.
- Backend title-search behavior passes against an in-memory SQLite database.
- Python compilation and repository whitespace checks pass.
- Browser-based screenshot QA could not be performed in the implementation session because no browser runtime was connected; production and source-level responsive checks were completed instead.
