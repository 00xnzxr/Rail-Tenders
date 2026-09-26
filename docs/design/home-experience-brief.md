# Home experience brief — universal landing screen

## Job of the screen

Within five seconds, a user should know:

1. What needs attention today.
2. Which tender is the best next opportunity.
3. Where to continue unfinished work.

Home is not a reporting dashboard. It is the user's daily tender desk.

## Desktop composition

```
┌───────────────┬───────────────────────────────────────────────┬─────────────────────┐
│ DRPL          │ Good morning, [name]                          │ Profile / alerts    │
│               │ “You have 3 things to do today.”              │                     │
│ Home          ├───────────────────────────────────────────────┼─────────────────────┤
│ Tenders       │ TODAY'S PRIORITIES                            │ CONTINUE WORKING    │
│ My Work       │ [Closing soon] [Ready to review] [In progress]│ tender/session card │
│ Ask DRPL      ├───────────────────────────────────────────────┤                     │
│               │ RECOMMENDED FOR YOU                           ├─────────────────────┤
│ Notifications │ tender card: fit, due date, why, one CTA      │ ASK DRPL            │
│               ├─────────────────────────┬─────────────────────┤ familiar starter    │
│ Settings      │ WORK WAITING FOR YOU     │ RECENT ACTIVITY     │ prompts             │
└───────────────┴─────────────────────────┴─────────────────────┴─────────────────────┘
```

The right rail collapses below large-desktop width and becomes an ordered section after the primary content. On mobile, Home is a single column ordered by urgency: priorities, recommendation, continue work, Ask DRPL, activity.

## Content hierarchy

### 1. Greeting and daily outcome

- Use the person's name when available.
- One plain-language sentence: “You have 2 tenders closing this week and 1 document ready to review.”
- Do not show raw system statistics in the greeting.

### 2. Today’s priorities

Use three compact action cards at most. Each card has a count, outcome statement, urgency/status label, and a direct destination.

| Situation | User-facing copy | Destination |
|---|---|---|
| Tender closes soon | “2 tenders need attention this week” | Closing Soon tender view |
| AI recommends a tender | “3 promising tenders are ready to review” | Recommended tender view |
| Work needs a decision | “1 document is ready for your approval” | My Work / document |

### 3. Recommended tender

Show one primary tender card, not a dense table. It needs:

- Tender title and issuing organization
- Due date, prominently stated in relative and exact form
- Fit/recommendation summary in one sentence
- The reason it was recommended (for example, “Matches your railway electrical-work profile”)
- One primary action: **Review tender**
- Secondary action: **Ask DRPL about this tender**

### 4. Continue working

Restore the exact thing the person last meaningfully worked on: tender review, checklist, document, or Command Center conversation. The button language names the next action, for example **Finish checklist** rather than **Open workspace**.

### 5. Ask DRPL

Keep this as a reassuring on-ramp, not a technical chat widget.

- Primary button: **Ask DRPL**
- Starter cards: “Explain a tender”, “Prepare a checklist”, “Draft a document”, “Estimate costs”
- When there is an active tender, add its context automatically and state that visibly.

## Interaction and motion

- Priority and tender cards are whole-card clickable, with clear text buttons retained for accessibility.
- Hover/focus: border becomes accent-tinted and elevation increases subtly over 180 ms.
- Counts animate once on initial load; never endlessly pulse.
- When new AI results arrive, the relevant card updates with a soft highlight and a text announcement, not a disruptive toast wall.
- “Ask DRPL” opens a contextual conversation transition that preserves the current tender/work context.
- All movement turns off or is reduced under `prefers-reduced-motion`.

## Empty and edge states

- **No tenders yet:** explain the import process and offer “Check portal connection” plus “Ask DRPL for help”.
- **No urgent work:** show reassurance, then recently added tenders or a short learning prompt.
- **Loading:** stable skeleton shapes preserve layout; do not replace the whole screen with a spinner.
- **Error:** identify the affected section and provide a local Retry action; keep other usable content visible.

## Data requirements to map before implementation

- Counts for closing soon, recommended/review-ready tenders, and waiting work
- Highest-priority recommended tender with reason and due date
- Last active tender, document, checklist, or Command Center session
- Pending approvals, signatures, and generated-document review states
- Notification counts relevant to immediate user action

## Acceptance criteria

- Every core card answers “what is this?”, “why does it matter?”, and “what do I do now?” without a tooltip.
- A user can enter the recommended tender journey or a contextual DRPL conversation in one click.
- No routine user is exposed to model, agent, pipeline, or artifact terminology.
- Keyboard and mobile flows retain the same task order and call-to-action clarity.
