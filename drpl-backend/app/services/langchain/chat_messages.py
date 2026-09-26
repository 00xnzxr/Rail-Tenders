"""The conversation, as a message list every provider will accept.

Two agents answer chat — the Master (`decision_maker`, the default
`chat_engine`) and the generalist — and each was assembling its own message
list. The Master's assembled nothing at all: it took `conversation_history`,
handed it to its worker tools, and invoked its graph with a single
`HumanMessage`, so the platform's assistant had no memory of the conversation.
The generalist did build one, correctly enough for the happy path and wrong in
the same two ways any hand-rolled version is wrong.

Both ways are about shape, not content, and both are invisible until a run
fails:

**A window can open on an assistant turn.** The transcript is sliced to the
last N turns; the slice lands wherever it lands. Anthropic and Google both
reject a request whose first message is an assistant message, so a memory
feature that ships without this check works until the conversation is long
enough to slice mid-exchange, and then 400s every message after that.

**A failed run leaves an unanswered user turn.** `streaming_handler` saves the
user turn as soon as the request arrives and the assistant turn only when the
run completes, so a timeout, a crash or a closed tab leaves a user turn with no
answer after it. The next message then produces two user messages in a row —
and Gemini rejects consecutive same-role messages outright. The failure mode is
therefore "one run failed, and now every subsequent message fails", which reads
as the platform breaking rather than one job going wrong.

So this module owns the shape, and states what it guarantees: the list starts
with a user message, alternates strictly, and ends with the message being
asked. Content rules — how many turns, how much of each — are budgets the
caller passes in.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger(__name__)

#: Roles that are the user speaking. Everything else in the transcript is the
#: platform answering: `save_conversation_turn` only ever writes "user" and
#: "assistant", and an unrecognised role is far likelier to be an agent's
#: output than something the user typed.
_USER_ROLES = frozenset({"user", "human"})


def _trim_turn(content: str, max_chars: int) -> str:
    """One turn, cut to budget, saying so.

    An assistant turn stores the entire answer it gave — after the worker
    handoff budget that can be twenty thousand characters — so a dozen
    unbudgeted turns are a larger prompt than the work they precede. A silent
    cut is worse than a short one: the model reads a truncated answer as the
    whole of what it said and contradicts itself defending it.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    dropped = len(content) - max_chars
    return (
        content[:max_chars]
        + f"\n\n[... {dropped:,} characters of this turn omitted ...]"
    )


def build_conversation_messages(
    conversation_history: Optional[list[dict]],
    current_message: str,
    *,
    max_turns: int,
    max_chars_per_turn: int,
    max_total_chars: int = 0,
) -> list[Any]:
    """Prior turns plus the message being asked, in a shape providers accept.

    Guarantees, in order of how badly each one fails without it:

    1. The list is never empty and always ends with ``current_message``.
    2. It begins with a user message.
    3. No two consecutive messages share a role — consecutive turns of the same
       role are joined into one message rather than dropped, because an
       unanswered question is part of what was said.
    4. The history costs at most ``max_total_chars`` (0 disables the ceiling).

    The total ceiling matters because the per-turn cap alone does not bound the
    prompt: a dozen turns each allowed four thousand characters is fifty
    thousand characters of history before the request is even read, and a
    conversation full of long answers hits that every time. The oldest turns
    are dropped first — the window's value is its recency, and what falls out
    of it is reachable with `conversation_history_search`.
    """
    messages: list[Any] = []

    if conversation_history and max_turns > 0:
        window = [
            turn for turn in conversation_history
            if isinstance(turn, dict) and (turn.get("content") or "").strip()
        ][-max_turns:]

        # Drop leading assistant turns: the window can open mid-exchange, and a
        # leading assistant message is rejected outright by some providers.
        while window and str(window[0].get("role", "")).lower() not in _USER_ROLES:
            window.pop(0)

        trimmed = [
            (
                str(turn.get("role", "")).lower() in _USER_ROLES,
                _trim_turn(str(turn.get("content") or ""), max_chars_per_turn),
            )
            for turn in window
        ]

        if max_total_chars > 0:
            # Drop oldest-first until the history fits its total budget.
            total = sum(len(content) for _, content in trimmed)
            while trimmed and total > max_total_chars:
                total -= len(trimmed[0][1])
                trimmed.pop(0)
            # Dropping may have exposed a leading assistant turn.
            while trimmed and not trimmed[0][0]:
                trimmed.pop(0)

        for is_user, content in trimmed:
            messages.append(
                HumanMessage(content=content) if is_user else AIMessage(content=content)
            )

    messages.append(HumanMessage(content=current_message))

    # Collapse consecutive same-role messages. The common case is a trailing
    # unanswered user turn from a run that failed, immediately followed by the
    # message the user is asking now.
    collapsed: list[Any] = []
    for message in messages:
        if collapsed and type(collapsed[-1]) is type(message):
            merged = f"{collapsed[-1].content}\n\n{message.content}"
            collapsed[-1] = type(message)(content=merged)
        else:
            collapsed.append(message)

    return collapsed
