import asyncio
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.domain.events import EventType, RunStatusPayload, StepStartedPayload
from app.domain.models import (
    Claim,
    ClaimType,
    ErrorCode,
    ReasonCode,
    RunStatus,
    Scale,
    StepName,
    Verdict,
    VerdictKind,
)
from app.storage.db import make_engine
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"
OTHER = "r_01jb7z00000000000000000001"


def _alembic(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    db_path = tmp_path / "p2p.db"
    command.upgrade(_alembic(db_path), "head")
    engine = make_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    yield Store(engine)
    await engine.dispose()


async def test_sqlite_pragmas(store: Store) -> None:
    async with store.engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1


async def test_create_get_and_update_run(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by="hash", claim_hint=None, budget={"x": 1})
    run = await store.get_run(RUN)
    assert run is not None
    assert (run.arxiv_id, run.status) == ("2106.09685", RunStatus.QUEUED)
    await store.set_run_status(RUN, RunStatus.FAILED, ErrorCode.NO_CLAIMS)
    run = await store.get_run(RUN)
    assert run is not None
    assert (run.status, run.error_code) == (RunStatus.FAILED, ErrorCode.NO_CLAIMS)
    assert run.finished_at is not None
    assert await store.get_run("r_" + "9" * 26) is None


async def test_events_after_returns_only_newer_events_in_order(store: Store) -> None:
    for run_id in (RUN, OTHER):
        await store.create_run(run_id, "2106.09685", created_by=None, claim_hint=None, budget={})
    ids = []
    for attempt in (1, 2, 3):
        ids.append(
            await store.append_event(
                RUN,
                EventType.STEP_STARTED,
                StepStartedPayload(step=StepName.PLANNING, attempt=attempt),
            )
        )
        await store.append_event(OTHER, EventType.RUN_STATUS, {"status": "RUNNING"})
    events = await store.events_after(RUN, ids[0])
    assert [e.id for e in events] == ids[1:]
    assert all(e.run_id == RUN for e in events)
    assert isinstance(events[0].payload, StepStartedPayload)
    assert events[0].payload.attempt == 2
    assert await store.events_after(RUN, ids[-1]) == []


async def test_append_event_validates_the_payload(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    with pytest.raises(ValueError):
        await store.append_event(RUN, EventType.STEP_STARTED, {"status": "RUNNING"})


async def test_concurrent_append_event_gives_distinct_increasing_ids(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    ids = await asyncio.gather(
        *(
            store.append_event(
                RUN, EventType.RUN_STATUS, RunStatusPayload(status=RunStatus.RUNNING)
            )
            for _ in range(20)
        )
    )
    assert len(set(ids)) == 20
    stored = [e.id for e in await store.events_after(RUN, 0)]
    assert stored == sorted(ids)
    assert all(b > a for a, b in itertools.pairwise(stored))


async def test_steps_are_upserted_by_run_step_attempt(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    await store.upsert_step(RUN, StepName.PROVISIONING, 1, "RUNNING")
    await store.upsert_step(RUN, StepName.PROVISIONING, 1, "SUCCEEDED", output_ref="s3://x")
    await store.upsert_step(RUN, StepName.PROVISIONING, 2, "RUNNING")
    steps = await store.get_steps(RUN)
    assert [(s.name, s.attempt, s.status) for s in steps] == [
        (StepName.PROVISIONING, 1, "SUCCEEDED"),
        (StepName.PROVISIONING, 2, "RUNNING"),
    ]
    assert steps[0].output_ref == "s3://x"
    assert steps[0].finished_at is not None


async def test_daily_spend_sums_model_calls_and_jobs_by_utc_date(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    day = datetime(2026, 10, 15, 23, 30, tzinfo=UTC)
    await store.record_model_call(RUN, "PLANNING", "planner", "m", 100, 10, 50, 0.25, at=day)
    await store.record_model_call(RUN, "PLANNING", "planner", "m", 100, 10, 50, None, at=day)
    await store.record_model_call(
        RUN, "PLANNING", "planner", "m", 1, 1, 1, 9.0, at=day + timedelta(hours=1)
    )
    await store.record_job(RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x", at=day)
    await store.update_job("p2p-exec-x-1", "SUCCEEDED", gpu_seconds=600, cost_usd=0.5)
    spend = await store.daily_spend(date(2026, 10, 15))
    assert spend.inference_usd == pytest.approx(0.25)
    assert spend.cloud_usd == pytest.approx(0.5)
    assert (await store.daily_spend(date(2026, 10, 16))).inference_usd == pytest.approx(9.0)


async def test_record_job_is_idempotent_by_name(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    first = await store.record_job(RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x")
    second = await store.record_job(RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x")
    assert first == second


async def test_claims_and_verdicts_round_trip(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    claim = Claim(
        id="c1",
        text="t",
        claim_type=ClaimType.ABSOLUTE,
        metric="accuracy",
        higher_is_better=True,
        dataset="d",
        setting="s",
        reported_value=0.9,
        source_ref="Table 1",
    )
    await store.save_claims(RUN, [claim], selected={"c1"})
    stored = await store.get_claims(RUN)
    assert stored == [(claim, True)]
    verdict = Verdict(
        kind=VerdictKind.INCONCLUSIVE,
        reason=ReasonCode.HIGH_VARIANCE,
        n_seeds=3,
        mean=0.8,
        std=0.1,
        ci_low=0.5,
        ci_high=1.1,
        tolerance={"w": 0.018, "v": 0.9},
    )
    await store.save_verdict(RUN, "c1", verdict, Scale.FULL, "abc", "explanation")
    assert await store.get_verdict(RUN, "c1") == verdict


def test_migrations_upgrade_and_downgrade_on_an_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    cfg = _alembic(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert {"runs", "claims", "steps", "events", "model_calls", "jobs", "verdicts"} <= tables
    assert {"invites", "ledger_daily"} <= tables
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def test_migration_matches_the_models(tmp_path: Path) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.storage.tables import Base

    db_path = tmp_path / "check.db"
    command.upgrade(_alembic(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []
