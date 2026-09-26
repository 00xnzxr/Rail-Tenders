# DRPL redesign design system

## Product and audience

DRPL helps teams discover, evaluate, prepare, and submit procurement tenders. Primary users are operations and bidding staff who may be unfamiliar with complex business software. The interface must feel guided, legible, and calm. Every screen must lead with a useful next action instead of its underlying data model or AI implementation.

## Visual character

Use the supplied Rayum and dark-workspace references as inspiration for composition and hierarchy only. DRPL remains its own brand: precise, trustworthy, and task-led.

- Light, cool-neutral page canvas; elevated white cards.
- Charcoal text with high contrast; muted slate secondary text.
- Slim gray borders and soft, low-contrast shadows.
- Large rounded cards (12–16px), compact rounded controls (8–10px).
- A single fresh emerald/green accent for primary recommendations and success; blue for navigational/data context; amber/red only for caution/risk.
- Use a clean sans-serif UI typeface already used by the application. Never introduce decorative, serif, or novelty fonts.

## Layout rules

- Persistent left navigation on desktop; responsive drawer on small screens.
- Home is the universal post-login landing page.
- Content uses a roomy 12-column desktop grid, 24px gutters, and 20–24px card gaps.
- Each card has one primary purpose and no more than one primary action.
- Information density may increase in detailed workspaces, but a plain-language summary and next step remain at the top.

## Navigation

Everyday navigation: Home, Tenders, My Work, Ask DRPL, Notifications. Settings and Help appear separately at the bottom. Role-gated platform administration is grouped and visually separated from everyday work.

The active navigation item uses a full-width tinted accent surface plus icon and label; inactive items remain quiet. Do not overload top-level navigation with administrative destinations.

## Component patterns

- **Priority card:** count, plain-language outcome, urgency badge, destination.
- **Recommended tender card:** title, organization, due date, recommendation reason, primary action.
- **Status:** always combine icon/color with a text label.
- **Action button:** direct verb phrase: Review tender, Continue checklist, Ask DRPL, View document.
- **AI progress:** plain-language current step plus a determinate or staged indicator. Avoid raw agent/pipeline jargon.
- **Drawers:** contextual, collapsible, keyboard-safe; used for tender context, AI files, and review actions.
- **Empty states:** explain why the state is empty and provide one suitable next step.

## Home content model

1. Greeting and short daily briefing.
2. Up to three urgency-ranked priority cards.
3. One recommended tender.
4. Continue-working card.
5. Contextual Ask DRPL starter panel.
6. Secondary recent activity only after actionable work.

## Command Center

Three-part desktop workspace: conversations/history, focused conversation, contextual tender/files/actions. The context panel collapses when not useful. Starter prompts use real tasks: explain tender, prepare checklist, draft document, estimate costs. AI plans are short, reviewable checklists with Start, Adjust, and Stop controls.

## Motion

- Navigation, tabs, and controls: 150–200ms.
- Card hover/focus: 180–240ms border/elevation transition.
- Page/view transitions: 200–280ms fade/short translation without moving essential controls.
- Drawers/modals: 220–280ms, focus management required.
- Animate data only on load or material update; never use continual motion as decoration.
- Respect `prefers-reduced-motion` and make every action usable without animation.

## Accessibility constraints

- WCAG-AA contrast minimum for normal text and controls.
- Keyboard focus is always visible.
- Touch targets are at least 44×44px where feasible.
- Do not use color as the sole indicator of urgency, completion, or selection.
- Keep user-facing language in short, direct sentences with familiar terms.

## Do not introduce

- Neon gradients, glassmorphism, decorative serif typography, generic SaaS illustrations, or oversized marketing hero sections.
- Hidden-only hover controls for essential actions.
- Technical labels such as agent, artifact, pipeline, model, or run in routine user workflows.
- Multiple equally prominent calls-to-action in the same component.
