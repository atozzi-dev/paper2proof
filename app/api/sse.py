"""Server-Sent Events: live stream with Last-Event-ID resumption, heartbeats, and replay."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from app.domain.events import EventEnvelope
from app.storage.store import Store

HEARTBEAT = b": hb\n\n"
MAX_REPLAY_GAP_S = 2.0


def format_event(event: EventEnvelope) -> bytes:
    data = json.dumps(event.payload.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type.value}\ndata: {data}\n\n".encode()


async def event_stream(
    store: Store,
    run_id: str,
    *,
    after_id: int,
    poll_s: float = 0.5,
    heartbeat_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    finished: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[bytes]:
    """Every stored event with id > after_id, then new ones as they arrive. Ends once
    `finished()` reports the run terminal and nothing is left to send."""
    last_id = after_id
    last_sent = clock()
    while True:
        events = await store.events_after(run_id, last_id)
        for event in events:
            yield format_event(event)
            last_id, last_sent = event.id, clock()
        if not events:
            if finished is not None and await finished():
                for event in await store.events_after(run_id, last_id):  # written meanwhile
                    yield format_event(event)
                return
            if clock() - last_sent >= heartbeat_s:
                yield HEARTBEAT
                last_sent = clock()
        await sleep(poll_s)


async def replay_stream(
    store: Store,
    run_id: str,
    *,
    speed: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[bytes]:
    """Re-emit stored events with their original gaps divided by `speed` (each capped at 2 s)."""
    previous = None
    for event in await store.events_after(run_id, 0):
        if previous is not None:
            gap = (event.ts - previous).total_seconds() / max(speed, 1e-6)
            await sleep(min(max(gap, 0.0), MAX_REPLAY_GAP_S))
        previous = event.ts
        yield format_event(event)
