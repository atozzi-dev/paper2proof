import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.orchestrator.budget import (
    Budget,
    BudgetExceeded,
    BudgetLimits,
    KillSwitchActive,
    SpendKind,
    load_budget_limits,
)
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"
RUN2 = "r_01jb7z00000000000000000001"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _limits() -> BudgetLimits:
    return load_budget_limits(ROOT / "config" / "budgets.yaml")


def _budget(store: Store, env: str = "test", now: datetime = NOW) -> Budget:
    return Budget(store, _limits(), env=env, now=lambda: now)  # type: ignore[arg-type]


async def _runs(store: Store) -> None:
    for run_id in (RUN, RUN2):
        await store.create_run(run_id, "2106.09685", created_by=None, claim_hint=None, budget={})


def test_limits_load_from_the_config_file() -> None:
    limits = _limits()
    assert limits.per_run.inference_usd == 1.00
    assert limits.global_.cloud_usd_per_day == 15.00
    assert limits.per_run.tavily_credits == 20


async def test_reservation_over_the_run_inference_cap_raises(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    with pytest.raises(BudgetExceeded) as err:
        await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 1.01)
    assert err.value.limit_name == "inference_usd"


async def test_in_flight_reservations_count_against_the_cap(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.6)
    with pytest.raises(BudgetExceeded):
        await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.6)


async def test_commit_below_the_reserve_frees_the_difference(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    reservation = await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.9)
    await budget.commit(reservation, 0.1)
    await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.85)  # 0.1 + 0.85 <= 1.0
    assert await budget.run_spend(RUN, SpendKind.INFERENCE_USD) == pytest.approx(0.1)
    with pytest.raises(BudgetExceeded):
        await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.1)


async def test_committed_spend_survives_a_restart(store: Store) -> None:
    await _runs(store)
    first_process = _budget(store)
    reservation = await first_process.check_and_reserve(RUN, SpendKind.GPU_MINUTES, 25)
    await first_process.commit(reservation, 25)
    with pytest.raises(BudgetExceeded) as err:
        await _budget(store).check_and_reserve(RUN, SpendKind.GPU_MINUTES, 6)  # new process
    assert err.value.limit_name == "gpu_minutes"


async def test_per_run_caps_are_per_run(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    await budget.commit(
        await budget.check_and_reserve(RUN, SpendKind.TOKENS_IN, 1_500_000), 1_500_000
    )
    await budget.check_and_reserve(RUN2, SpendKind.TOKENS_IN, 1_000_000)
    with pytest.raises(BudgetExceeded) as err:
        await budget.check_and_reserve(RUN, SpendKind.TOKENS_IN, 1)
    assert err.value.limit_name == "tokens_in"


async def test_daily_cloud_cap_flips_the_kill_switch(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    reservation = await budget.check_and_reserve(RUN, SpendKind.CLOUD_USD, 14.0)
    await budget.commit(reservation, 15.0)  # actual above the reserve still counts
    assert await budget.kill_switch_active()
    with pytest.raises(KillSwitchActive):
        await budget.check_and_reserve(RUN2, SpendKind.GPU_MINUTES, 1)
    with pytest.raises(KillSwitchActive):
        await budget.check_and_reserve(RUN2, SpendKind.CLOUD_USD, 0.01)
    # Model calls for runs already in flight still pass.
    await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.1)
    # The switch is per UTC day and persisted.
    assert await _budget(store).kill_switch_active()
    tomorrow = _budget(store, now=datetime(2026, 10, 21, 0, 1, tzinfo=UTC))
    assert not await tomorrow.kill_switch_active()


async def test_reservation_over_the_daily_cloud_cap_raises(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    with pytest.raises(BudgetExceeded) as err:
        await budget.check_and_reserve(None, SpendKind.CLOUD_USD, 15.5)
    assert err.value.limit_name == "cloud_usd_per_day"


async def test_manual_kill_switch(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    await budget.set_kill_switch(True)
    with pytest.raises(KillSwitchActive):
        await budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, 1)
    await budget.set_kill_switch(False)
    await budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, 1)


async def test_dev_inference_cap_applies_only_in_dev(store: Store) -> None:
    await _runs(store)
    for env in ("test", "prod"):
        await _budget(store, env=env).check_and_reserve(None, SpendKind.INFERENCE_USD, 2.5)
    with pytest.raises(BudgetExceeded) as err:
        await _budget(store, env="dev").check_and_reserve(None, SpendKind.INFERENCE_USD, 2.5)
    assert err.value.limit_name == "dev_inference_usd_per_day"


async def test_daily_inference_cap_sums_all_runs(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    for run_id in (RUN, RUN2):
        for _ in range(3):
            await budget.commit(
                await budget.check_and_reserve(run_id, SpendKind.INFERENCE_USD, 0.3), 0.3
            )
    assert await budget.day_spend(SpendKind.INFERENCE_USD) == pytest.approx(1.8)
    await budget.commit(await budget.check_and_reserve(None, SpendKind.INFERENCE_USD, 4.0), 4.0)
    with pytest.raises(BudgetExceeded) as err:
        await budget.check_and_reserve(None, SpendKind.INFERENCE_USD, 0.3)
    assert err.value.limit_name == "inference_usd_per_day"


async def test_tavily_credits_are_capped_per_run(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    with pytest.raises(BudgetExceeded) as err:
        await budget.check_and_reserve(RUN, SpendKind.TAVILY_CREDITS, 21)
    assert err.value.limit_name == "tavily_credits"


async def test_concurrent_reservations_never_overshoot(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)

    async def take() -> bool:
        try:
            await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.3)
        except BudgetExceeded:
            return False
        return True

    granted = await asyncio.gather(*(take() for _ in range(10)))
    assert sum(granted) == 3  # 3 x 0.3 <= 1.0 < 4 x 0.3


async def test_negative_amounts_are_rejected(store: Store) -> None:
    budget = _budget(store)
    with pytest.raises(ValueError):
        await budget.check_and_reserve(None, SpendKind.INFERENCE_USD, -1)


async def test_commit_is_single_use(store: Store) -> None:
    await _runs(store)
    budget = _budget(store)
    reservation = await budget.check_and_reserve(RUN, SpendKind.INFERENCE_USD, 0.2)
    await budget.commit(reservation, 0.2)
    with pytest.raises(ValueError):
        await budget.commit(reservation, 0.2)


async def test_spend_is_attributed_to_the_utc_day(store: Store) -> None:
    await _runs(store)
    late_evening_utc_minus_2 = datetime(2026, 10, 20, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    budget = _budget(store, now=late_evening_utc_minus_2)
    reservation = await budget.check_and_reserve(RUN, SpendKind.CLOUD_USD, 1.0)
    assert reservation.day == date(2026, 10, 21)
