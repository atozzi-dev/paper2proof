import asyncio
import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
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
from app.storage.store import JOB_ACTIVE, TERMINAL_STEP_STATUSES, Store
from tests.conftest import alembic_config as _alembic

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"
OTHER = "r_01jb7z00000000000000000001"


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


async def test_record_job_keeps_where_the_job_runs(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    await store.record_job(
        RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x",
        platform="gpu-l40s-d", preset="1gpu-16vcpu-96gb",
    )  # fmt: skip
    await store.record_job(RUN, "exec", "p2p-exec-x-2", "job-2", "p2p-run-x")
    first, second = await store.get_job("p2p-exec-x-1"), await store.get_job("p2p-exec-x-2")
    assert first is not None and (first.platform, first.preset) == (
        "gpu-l40s-d",
        "1gpu-16vcpu-96gb",
    )
    assert second is not None and (second.platform, second.preset) == (None, None)


async def test_get_job_returns_the_first_submission(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    at = datetime(2026, 10, 15, 12, tzinfo=UTC)
    assert await store.get_job("p2p-exec-x-1") is None
    await store.record_job(RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x", at=at)
    await store.record_job(RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x")  # a retry
    job = await store.get_job("p2p-exec-x-1")
    assert job is not None
    assert (job.name, job.kind, job.status, job.created_at) == (
        "p2p-exec-x-1",
        "exec",
        "SUBMITTED",
        at,
    )


async def test_a_job_leaving_the_queue_is_marked_active_once(store: Store) -> None:
    """The deadline then counts from activation; a settled job is never reopened."""
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    at = datetime(2026, 10, 15, 12, tzinfo=UTC)
    await store.record_job(RUN, "exec", "p2p-exec-x-1", "job-1", "p2p-run-x", at=at)
    first = at + timedelta(minutes=40)
    assert await store.mark_job_active("p2p-exec-x-1", deadline=first)
    assert not await store.mark_job_active("p2p-exec-x-1", deadline=first + timedelta(hours=1))
    job = await store.get_job("p2p-exec-x-1")
    assert job is not None and (job.status, job.deadline) == (JOB_ACTIVE, first)
    await store.update_job("p2p-exec-x-1", "SUCCEEDED")
    assert not await store.mark_job_active("p2p-exec-x-1", deadline=first)
    assert JOB_ACTIVE not in TERMINAL_STEP_STATUSES  # the reaper still settles an active job


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


async def test_a_run_keeps_the_title_of_its_paper(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    run = await store.get_run(RUN)
    assert run is not None and run.title is None
    await store.set_title(RUN, "LoRA: Low-Rank Adaptation of Large Language Models")
    run = await store.get_run(RUN)
    assert run is not None and run.title == "LoRA: Low-Rank Adaptation of Large Language Models"
    await store.set_listed(RUN, True)
    assert [r.title for r in await store.list_gallery()] == [run.title]


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


def test_overlay_digests_from_the_single_provision_job_are_dropped(tmp_path: Path) -> None:
    # recorded while the job that wrote the cache also parsed untrusted datasets (A-06)
    db_path = tmp_path / "trust.db"
    cfg = _alembic(db_path)
    command.upgrade(cfg, "0005")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO overlay_digests (cache_key, sha256, run_id, created_at)"
                " VALUES (:k, :s, NULL, '2026-10-15 10:00:00')"
            ),
            {"k": "a" * 64, "s": "b" * 64},
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM overlay_digests")).scalar() == 0
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


async def test_cancel_wins_over_a_late_begin_or_transition(store: Store) -> None:
    from app.storage.store import StateConflict

    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    assert await store.cancel_run(RUN)
    with pytest.raises(StateConflict):
        await store.begin_run(RUN, StepName.INGESTING, 1)
    run = await store.get_run(RUN)
    assert run is not None and run.status is RunStatus.CANCELLED
    assert not await store.cancel_run(RUN)  # already terminal


async def test_trusted_overlays_first_writer_wins_newest_first(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    assert await store.trusted_overlays() == {}
    t0 = datetime(2026, 10, 15, 12, tzinfo=UTC)
    await store.trust_overlay("a" * 64, "1" * 64, RUN, at=t0)
    await store.trust_overlay("a" * 64, "2" * 64, RUN, at=t0 + timedelta(hours=1))  # ignored
    await store.trust_overlay("b" * 64, "3" * 64, RUN, at=t0 + timedelta(hours=2))
    assert await store.trusted_overlays() == {"a" * 64: "1" * 64, "b" * 64: "3" * 64}
    assert await store.trusted_overlays(limit=1) == {"b" * 64: "3" * 64}


GPU_INVITE = "9f" * 32
GPU_DAY = datetime(2026, 11, 16, 9, tzinfo=UTC)


async def _gpu_run(store: Store, run_id: str = RUN, invite: str | None = GPU_INVITE) -> None:
    await store.create_run(
        run_id, "2106.09685", created_by="sid", claim_hint=None, budget={}, invite_hash=invite
    )


async def test_a_run_keeps_the_invite_it_was_created_with(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=1)
    await _gpu_run(store)
    run = await store.get_run(RUN)
    assert run is not None and run.invite_hash == GPU_INVITE


async def test_an_invite_without_gpu_runs_gets_no_gpu(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None)  # the default: CPU only
    await _gpu_run(store)
    assert not await store.consume_gpu_run(RUN, GPU_DAY)


async def test_gpu_runs_are_counted_per_utc_day(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=1)
    runs = [f"r_01jb7z000000000000000000{n}" for n in ("0a", "0b", "0c")]
    for run_id in runs:
        await _gpu_run(store, run_id)
    assert await store.consume_gpu_run(runs[0], GPU_DAY)
    assert not await store.consume_gpu_run(runs[1], GPU_DAY + timedelta(hours=1))
    assert await store.consume_gpu_run(runs[2], GPU_DAY + timedelta(days=1))


async def test_an_expired_invite_gets_no_gpu(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, GPU_DAY - timedelta(days=1), gpu_runs_per_day=5)
    await _gpu_run(store)
    assert not await store.consume_gpu_run(RUN, GPU_DAY)


async def test_a_run_without_an_invite_gets_no_gpu(store: Store) -> None:
    """Runs created before A-47 have no invite_hash; an unknown run has nothing at all."""
    await _gpu_run(store, invite=None)
    assert not await store.consume_gpu_run(RUN, GPU_DAY)
    assert not await store.consume_gpu_run("r_" + "9" * 26, GPU_DAY)


async def test_two_runs_racing_for_the_last_gpu_run_get_one(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=1)
    runs = ["r_01jb7z0000000000000000000a", "r_01jb7z0000000000000000000b"]
    for run_id in runs:
        await _gpu_run(store, run_id)
    got = await asyncio.gather(*(store.consume_gpu_run(r, GPU_DAY) for r in runs))
    assert sorted(got) == [False, True]


async def test_the_gpu_quota_is_listed_with_the_invite(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=2)
    await _gpu_run(store)
    await store.consume_gpu_run(RUN, GPU_DAY)
    [invite] = [i for i in await store.list_invites() if i.code_hash == GPU_INVITE]
    assert (invite.gpu_runs_per_day, invite.gpu_used_today, invite.gpu_used_on) == (
        2,
        1,
        GPU_DAY.date(),
    )


async def test_a_run_takes_one_gpu_run_however_often_it_asks(store: Store) -> None:
    """Minor 4: the grant is recorded on the run in the same transaction: a resumed PLANNING
    gets it again, never a second one."""
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=2)
    await _gpu_run(store)
    assert await store.consume_gpu_run(RUN, GPU_DAY)
    assert await store.consume_gpu_run(RUN, GPU_DAY)
    [invite] = [i for i in await store.list_invites() if i.code_hash == GPU_INVITE]
    assert invite.gpu_used_today == 1


async def test_the_gpu_quota_of_a_run_s_invite(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=2)
    await _gpu_run(store)
    assert await store.gpu_quota(RUN) == 2
    await _gpu_run(store, "r_01jb7z0000000000000000000c", invite=None)
    assert await store.gpu_quota("r_01jb7z0000000000000000000c") == 0


async def test_a_gpu_run_refused_by_the_invite_leaves_the_run_without_a_grant(
    store: Store,
) -> None:
    await store.create_invite(GPU_INVITE, 3, None)  # no GPU runs
    await _gpu_run(store)
    assert not await store.consume_gpu_run(RUN, GPU_DAY)
    await store.create_invite("8e" * 32, 3, None, gpu_runs_per_day=1)
    run = await store.get_run(RUN)
    assert run is not None
    assert not await store.consume_gpu_run(RUN, GPU_DAY)  # still refused: no grant was kept


async def test_two_tries_of_one_run_racing_take_one_gpu_run(store: Store) -> None:
    await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=2)
    await _gpu_run(store)
    got = await asyncio.gather(*(store.consume_gpu_run(RUN, GPU_DAY) for _ in range(4)))
    assert all(got)
    [invite] = [i for i in await store.list_invites() if i.code_hash == GPU_INVITE]
    assert invite.gpu_used_today == 1


async def test_a_read_cut_off_by_a_cancel_scope_leaves_the_pool_usable(store: Store) -> None:
    """Starlette stops an SSE stream with an anyio cancel scope when the client goes away, and
    that scope cancels every await inside it, the pool's own cleanup included. A cut-off read
    must neither leave a dead connection in the pool ("no active connection" on the next
    request) nor hang."""
    import anyio

    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})
    for i in range(200):
        with anyio.move_on_after((i % 40) * 0.0001):
            await store.events_after(RUN, 0)
        with anyio.fail_after(3):
            assert await store.get_run(RUN) is not None
