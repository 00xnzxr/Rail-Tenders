"""Run an agent stream that outlives its HTTP request.

An SSE endpoint that drives a run directly from its response generator ties the
run's lifetime to the client's attention span. When the browser goes away the
generator is cancelled — and because the assistant's reply is persisted from
*inside* the run, cancelling it discards the reply, the work, and the API spend.
The session then looks exactly as though nothing had been sent. That is what a
"blank session" was.

`detached_stream` breaks that coupling. The run owns itself and pushes events
into a queue the response drains. If the client leaves, the response ends and
the run keeps going to completion, so the answer is still saved and appears on
reload.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: Detached runs, held while in flight. asyncio keeps only weak references to
#: tasks, so an untracked one can be garbage collected mid-run — which would
#: quietly reintroduce the bug this module exists to fix.
_IN_FLIGHT: set[asyncio.Task] = set()


def track(task: asyncio.Task, label: str) -> None:
    """Hold a detached run and make sure its failures are logged, not swallowed."""
    _IN_FLIGHT.add(task)

    def _done(finished: asyncio.Task) -> None:
        _IN_FLIGHT.discard(finished)
        if finished.cancelled():
            logger.warning("Detached run %s was cancelled", label)
            return
        exc = finished.exception()
        if exc:
            logger.error("Detached run %s failed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_done)


def in_flight_count() -> int:
    return len(_IN_FLIGHT)


async def detached_stream(
    source_factory: Callable[[], AsyncIterator[str]],
    label: str,
    on_error: Optional[Callable[[Exception], list[str]]] = None,
) -> AsyncIterator[str]:
    """Yield events from `source_factory()` without tying it to the consumer.

    The queue is unbounded on purpose: a put must never block on a consumer
    that has stopped reading. Runs are bounded by their own timeouts, so it
    cannot grow without limit.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _run_to_completion() -> None:
        try:
            async for event in source_factory():
                queue.put_nowait(event)
        except Exception as e:  # noqa: BLE001 — surfaced to the client below
            logger.error("Detached run %s failed: %s", label, e, exc_info=True)
            for event in (on_error(e) if on_error else []):
                queue.put_nowait(event)
        finally:
            queue.put_nowait(None)

    task = asyncio.ensure_future(_run_to_completion())
    track(task, label)

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    except (asyncio.CancelledError, GeneratorExit):
        # The client is gone. Deliberately do NOT cancel the task: the run
        # finishes and its reply is still written, so the user sees it on
        # reload instead of losing the whole turn.
        logger.warning(
            "Client disconnected from %s — the run continues in the background "
            "and its reply will still be saved.", label,
        )
        raise
