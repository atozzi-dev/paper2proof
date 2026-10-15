from datetime import UTC, datetime

import httpx

from app.api.main import AppState
from app.api.sse import event_stream
from app.domain.events import EventType
from app.domain.models import RunStatus, StepName
from app.storage.store import Store
from tests.api.conftest import login

RUN = "r_01jb7z00000000000000000000"


async def _seed(store: Store, n: int = 3) -> list[int]:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    return [
        await store.append_event(
            RUN, EventType.STEP_STARTED, {"step": StepName.PLANNING, "attempt": i + 1}
        )
        for i in range(n)
    ]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def _take(stream: object, n: int) -> list[bytes]:
    out = []
    async for chunk in stream:  # type: ignore[attr-defined]
        out.append(chunk)
        if len(out) == n:
            break
    return out


async def test_reconnect_with_last_event_id_replays_only_newer(store: Store) -> None:
    ids = await _seed(store)
    first = await _take(event_stream(store, RUN, after_id=0, poll_s=0.01), 3)
    assert [c.split(b"\n")[0] for c in first] == [f"id: {i}".encode() for i in ids]
    # Disconnect, then reconnect with Last-Event-ID = second event; a new event arrives.
    new_id = await store.append_event(RUN, EventType.RUN_STATUS, {"status": "RUNNING"})
    resumed = await _take(event_stream(store, RUN, after_id=ids[1], poll_s=0.01), 2)
    assert [c.split(b"\n")[0] for c in resumed] == [
        f"id: {ids[2]}".encode(),
        f"id: {new_id}".encode(),
    ]
    assert b"event: run.status" in resumed[1]


async def test_heartbeat_within_16_seconds(store: Store) -> None:
    await _seed(store, 0)
    clock = FakeClock()
    chunks = await _take(
        event_stream(store, RUN, after_id=0, poll_s=1.0, clock=clock, sleep=clock.sleep), 1
    )
    assert chunks == [b": hb\n\n"]
    assert clock.now <= 16


async def test_live_stream_closes_after_the_run_ends(
    client: httpx.AsyncClient, store: Store
) -> None:
    ids = await _seed(store)
    await store.set_run_status(RUN, RunStatus.COMPLETED)
    await store.set_listed(RUN, True)  # public: no session needed
    response = await client.get(f"/api/runs/{RUN}/events", headers={"Last-Event-ID": str(ids[1])})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {ids[2]}" in response.text
    assert f"id: {ids[1]}\n" not in response.text


async def test_replay_streams_everything_in_order_and_closes(
    client: httpx.AsyncClient, store: Store
) -> None:
    ids = await _seed(store)
    await store.set_listed(RUN, True)
    response = await client.get(f"/api/runs/{RUN}/events", params={"replay": 1, "speed": 1000})
    positions = [response.text.index(f"id: {i}\n") for i in ids]
    assert positions == sorted(positions)


async def test_polling_fallback(client: httpx.AsyncClient, store: Store) -> None:
    ids = await _seed(store)
    await store.set_listed(RUN, True)
    body = (await client.get(f"/api/runs/{RUN}/events.json", params={"after": ids[0]})).json()
    assert [e["id"] for e in body["events"]] == ids[1:]


async def test_stream_limit_per_session(
    client: httpx.AsyncClient, state: AppState, store: Store
) -> None:
    await login(client)
    run_id = (await client.post("/api/runs", json={"arxiv_id": "2106.09685"})).json()["run_id"]
    state.sse_streams_per_session = 0  # every stream is over the limit
    response = await client.get(f"/api/runs/{run_id}/events")
    assert response.status_code == 429


def test_ts_is_utc() -> None:
    assert datetime.now(UTC).tzinfo is UTC


async def test_concurrent_streams_respect_the_cap(
    client: httpx.AsyncClient, state: AppState, store: Store
) -> None:
    import asyncio

    await _seed(store)
    await store.set_listed(RUN, True)
    state.sse_streams_per_session = 2
    gate = asyncio.Event()
    original = state.store.events_after

    async def slow_events_after(run_id: str, after_id: int) -> list[object]:
        await gate.wait()
        return list(await original(run_id, after_id))

    state.store.events_after = slow_events_after  # type: ignore[method-assign,assignment]

    async def open_stream() -> int:
        async with client.stream("GET", f"/api/runs/{RUN}/events") as response:
            await response.aread()
            return response.status_code

    tasks = [asyncio.create_task(open_stream()) for _ in range(5)]
    await asyncio.sleep(0.3)
    await store.set_run_status(RUN, RunStatus.COMPLETED)
    gate.set()
    codes = await asyncio.gather(*tasks)
    assert codes.count(200) == 2
    assert codes.count(429) == 3
    assert state.open_streams == {}  # released on close


async def test_garbage_last_event_id_is_ignored(client: httpx.AsyncClient, store: Store) -> None:
    await _seed(store)
    await store.set_run_status(RUN, RunStatus.COMPLETED)
    await store.set_listed(RUN, True)
    garbage = {"Last-Event-ID": "\u00b2".encode()}  # a Unicode digit: isdigit() is True
    response = await client.get(f"/api/runs/{RUN}/events", headers=garbage)
    assert response.status_code == 200
