# DRPL Digital Assistant Experience

## Product role

The assistant is a platform-wide working partner, not a passive help widget. It can answer general questions, understand the current page and tender, inspect platform data, recommend tenders the company can bid on, explain eligibility and next steps, prepare tender artifacts, and carry work into the full Command Center.

## Experience principles

- Friendly and calm: human, concise language; no technical agent jargon in the primary UI.
- Visible progress: always show what is happening now, what has completed, what comes next, and whether the user can safely leave the panel.
- Trust through control: read-only work can proceed automatically; consequential actions appear as explicit approval cards with impact and destination.
- Continuous context: the floating assistant and Command Center use the same selected conversation. Moving between them never loses messages, tender context, artifacts, or progress; “New chat” deliberately starts a separate saved context.
- Durable history: chats are stored as searchable sessions grouped into Today, Previous 7 days, and Older, with title, tender link, last activity, and running/completed state.

## Floating assistant states

### Ready

- Warm greeting: “Hi Admin — what would you like to move forward?”
- Context chip such as “Home” or a linked tender name.
- Primary proactive card: “I found 6 tenders worth reviewing” with last scan time and a Review matches action.
- Short task starters: Find tenders we can bid on; Check eligibility; Explain how to proceed; Prepare a checklist.
- History control in the header and a clear “Open Command Center” link.

### Working

- Plain-language live status, e.g. “Checking 65 current tenders against your company profile”.
- Compact step list with complete, active, and queued states: Reading tender notices; Checking eligibility; Comparing deadlines and value; Preparing shortlist.
- Progress count and elapsed time when useful, plus “You can close this — I’ll keep working.”
- Stream useful partial results rather than an empty loading state.

### Needs a decision

- A short result summary followed by an approval card.
- Explain the exact proposed action, affected tender/document, reversibility, and outcome.
- Clear Approve and Not now controls; never hide the approval in technical detail.

### Complete

- Outcome-first summary: what was found or created.
- Result cards link to tenders, checklists, costing, or documents.
- Suggested next step and Open in Command Center action.

## Command Center

- Left rail: New chat, search, filters, and stored conversation history with status dots and tender links.
- Main chat: same friendly assistant identity, retained page/tender context, task starters, messages, approvals, and artifacts.
- A persistent activity header explains the current run in one sentence.
- A collapsible “Work log” shows readable steps and tool details separately.
- Right workspace rail displays generated checklists, analyses, cost estimates, and documents without interrupting chat.
- General questions and non-tender work are supported; tender context is optional, visible, and removable.

## Visual direction

Keep the established DRPL enterprise system and restrained emerald accent. Make the assistant warmer through a soft mint-to-white header wash, a small friendly sparkle/orbit mark, rounded 14–16px cards, conversational copy, generous message spacing, and subtle status animation. Avoid mascot illustrations, neon gradients, oversized chat bubbles, and opaque “thinking” language.
