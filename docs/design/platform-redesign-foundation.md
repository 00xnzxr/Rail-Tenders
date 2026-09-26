# DRPL platform redesign foundation

## Purpose

DRPL is an AI-powered tender intelligence platform. It receives tender data from procurement portals, makes it searchable and scoreable, helps users decide what to pursue, and supports the work that follows: analysis, checklists, document preparation, signing, and review.

The redesign must make that power feel calm and approachable for people who are not comfortable with complex software. The product should guide a user toward the next sensible action rather than ask them to understand DRPL's internal workflow, AI agents, or data model.

## Evidence reviewed

- Current React/Vite application routes, shell, dashboard, Tenders, Tender Detail, and Command Center implementation.
- Backend route surface for tender search, scoring, document analysis, checklists, workspace artifacts, offline documents, signatures, and Command Center streaming.
- Four supplied visual references.

## Current product map

### Everyday work

1. **Start / Dashboard** — see the immediate workload and recent activity.
2. **Tenders** — browse, search, filter, score, and choose a tender.
3. **Tender workspace** — understand a tender, review risk, complete the checklist, and prepare files.
4. **Command Center** — ask for help with analysis, drafts, costs, documents, or a tender workspace; view streaming progress and generated artifacts.
5. **Documents and signatures** — prepare, sign, and manage output.

### Administrative work

Portal monitoring, reviews, users, platform settings, scoring, security, audit logs, templates, agent configuration, workflows, data sets, ratecards, and batch processing.

## Design direction inferred from the references

The references point to a **quiet, card-led desktop workspace** rather than a dense enterprise control panel.

- Permanent left navigation with a highly visible active state; only core destinations live at the top level.
- Bright neutral canvas, white surfaces, thin cool-gray outlines, generous corner radii, and restrained shadow.
- One vivid green is used sparingly to communicate success, recommendation, or a primary action. Blue supports navigation/data; amber and red remain reserved for caution and risk.
- Dense information is broken into small, independently understandable cards with one action or outcome each.
- Dark mode is a first-class theme, not a recolored light layout. It should use layered charcoal surfaces and a disciplined accent palette, matching the fourth reference's focus and contrast.
- The Command Center should borrow the second reference's three-part composition: navigation/history, focused conversation, and a contextual action/details drawer. It should feel like a guided work assistant, not an engineering console.

## UX principles

1. **Guide, do not expose machinery.** Use human labels such as “Review tender”, “Prepare submission”, and “Ask DRPL” instead of internal agent, pipeline, or artifact terminology.
2. **One obvious next action.** Every landing and detail screen has a primary next step, plus at most two secondary choices.
3. **Progress is visible and reassuring.** AI work must say what is happening in plain language, estimate/sequence work when possible, and let users safely continue browsing.
4. **Progressive disclosure.** Keep routine screens simple. Move bulk actions, secondary filters, raw AI details, and admin controls behind clear drawers, sections, or an admin workspace.
5. **Recognizable status language.** Use a consistent tender lifecycle: New, Review, Pursue, Preparing, Ready to submit, Submitted, Closed. Never rely on color alone.
6. **Forgiving interactions.** Confirm destructive actions, support undo where feasible, remember choices, and make empty/error states tell the user exactly what to do next.
7. **Accessible motion.** Motion reinforces cause and effect; it never delays work. Respect `prefers-reduced-motion`, retain keyboard access, and keep contrast/text size practical.

## Proposed information architecture

### Main navigation

- **Home** — today’s priorities, work in progress, and simple outcomes.
- **Tenders** — all tender discovery and decisions, with saved views instead of a long filter wall.
- **My Work** — documents, checklists, approvals, and items awaiting action.
- **Ask DRPL** — Command Center, conversations, and generated workspaces.
- **Notifications** — only when there is something requiring attention.

Settings and Help sit at the bottom. Administrative configuration is a role-gated “Platform administration” area with its own grouped navigation, not part of everyday task flow.

**Landing rule:** Every user lands on **Home** after sign-in. Home is the calm orientation point and directs people to the next task; Command Center is entered deliberately from its primary “Ask DRPL” action or from a tender already in context.

### Core task flow

`Home → choose priority → Tender overview → Review findings → Prepare submission → Track completion`

At every stage, “Ask DRPL” should retain the tender context automatically so a user never has to restate what they are working on.

## Key screen concepts

### Home: “Today’s tender desk”

- Greeting plus a brief plain-language daily summary.
- Priority cards: closing soon, high-fit tenders to review, and work waiting for the user.
- A compact trend/health summary only when it changes a decision.
- “Continue where you left off” card for the active tender or Command Center session.

### Tenders: decision board, not a database

- Keep the existing title search, with highlighted result terms.
- Replace dense filter-first browsing with named views: **Recommended**, **Needs review**, **Closing soon**, **New**, and **All tenders**.
- Tender rows/cards lead with fit, due date, organization, and one recommended action.
- Detail opens into a purposeful overview: decision summary, key risks, required documents, and next action before raw source data.

### Command Center: guided AI workspace

- Left: recent conversations, simple search, and “New request”.
- Center: focused conversation with a welcoming empty state and task starter cards in plain language.
- Right: contextual drawer for the active tender, generated files, plan/status, or approval request; collapse it when not needed.
- Replace technical stream/pipeline jargon with messages such as “Reading 3 tender documents” and “Preparing your checklist”.
- Present AI plans as short reviewable checklists with clear **Start**, **Adjust**, and **Stop** choices.

### My Work

- One queue for documents to review, checklist items, signatures, approvals, and submission-ready work.
- Group by urgency and task, not by the backend subsystem that produced the item.

## Motion system

Use CSS/React animation primitives already compatible with the application; add a dedicated motion library only after the prototype validates a need.

- Navigation and tabs: 150–200 ms position/color transitions.
- Cards: 180–240 ms elevation/border response on hover; no excessive lift on touch devices.
- View changes: short fade/slide (200–280 ms) that preserves location and avoids disorientation.
- AI progress: calm, determinate steps and gentle shimmer only while work is actually pending.
- Numbers/charts: animate only when newly loaded or materially changed; provide text equivalents.
- Drawers/modals: 220–280 ms spring-like transition, focus trapped and escape-safe.

## Delivery sequence

1. **Foundation** — tokens, type scale, spacing, icon rules, light/dark themes, responsive shell, motion/accessibility rules.
2. **Core navigation + Home** — validate the new mental model with non-technical users.
3. **Tenders + Tender overview** — redesign discovery, decision states, and next-action hierarchy.
4. **Command Center** — redesign conversation, session handling, AI progress, approvals, and artifact workspace.
5. **My Work + documents** — unify follow-up tasks, checklists, documents, and signatures.
6. **Admin workspace** — simplify role-gated configuration without exposing it to everyday users.
7. **Validation + rollout** — keyboard/mobile QA, reduced-motion QA, usability sessions, analytics events, staged release.

## Decisions to validate before visual implementation

- Which user roles are primary in the first redesign release: bidder/operator, manager, or platform admin?
- Which actions are legally or operationally sensitive enough to require a confirmation/approval step?
- Which desktop width and mobile/tablet workflows must be supported at launch?
- What real user language should replace the current internal labels (for example, “artifact”, “pipeline”, and “agent”)?

## Definition of success

- A first-time user can find a recommended tender, understand why it matters, and start the next action without training.
- A user can ask DRPL for help and understand its progress, result, and required decision.
- Routine users see no admin controls or technical jargon.
- The UI remains fast, keyboard-accessible, readable, and usable with reduced motion.
