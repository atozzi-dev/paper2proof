"""Metrics of architecture §14: registered once, recorded with the right labels."""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.observability import metrics
from app.settings import Settings
from tests.observability.fakes import Telemetry

ROOT = Path(__file__).resolve().parents[2]


def test_metrics_are_registered_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "_current", None)
    first = metrics.instruments()
    assert metrics.instruments() is first


def test_no_meter_provider_in_test_or_without_an_endpoint() -> None:
    test = Settings(p2p_env="test", p2p_metrics_endpoint="https://collector.example.com:4317")
    assert metrics.build_meter_provider(test) is None
    dev = Settings(p2p_env="dev")
    assert metrics.build_meter_provider(dev) is None


def test_a_prod_meter_provider_is_built_for_an_endpoint() -> None:
    settings = Settings(
        p2p_env="prod",
        nebius_api_key=SecretStr("n" * 40),
        session_key=SecretStr("s" * 40),
        admin_token=SecretStr("a" * 40),
        p2p_metrics_endpoint="https://collector.example.com:4317",
    )
    provider = metrics.build_meter_provider(settings)
    assert provider is not None
    provider.shutdown()


def test_recorders_use_the_architecture_names(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = Telemetry(monkeypatch)
    metrics.run_ended("COMPLETED")
    metrics.run_driven(+1)
    metrics.step_finished("PLANNING", 2.5)
    metrics.model_call("planner", "vendor/model-x", tokens_in=100, tokens_out=20, remaining=900)
    metrics.spend("inference_usd", 0.25)
    metrics.spend("cloud_usd", 1.5)
    metrics.spend("gpu_minutes", 2.0)
    metrics.spend("tokens_in", 100.0)  # counted by model_call with its labels, not here
    assert telemetry.points("p2p_runs_total") == {(("status", "COMPLETED"),): 1}
    assert telemetry.points("p2p_runs_active") == {(): 1}
    assert telemetry.points("p2p_step_duration_seconds") == {(("step", "PLANNING"),): 1}
    assert telemetry.points("p2p_model_tokens_total") == {
        (("dir", "in"), ("model", "vendor/model-x"), ("role", "planner")): 100,
        (("dir", "out"), ("model", "vendor/model-x"), ("role", "planner")): 20,
    }
    assert telemetry.points("p2p_ratelimit_remaining_tokens") == {
        (("model", "vendor/model-x"),): 900
    }
    assert telemetry.points("p2p_cost_usd_total") == {
        (("kind", "inference"),): 0.25,
        (("kind", "cloud"),): 1.5,
    }
    assert telemetry.points("p2p_gpu_seconds_total") == {(): 120.0}


def test_priming_creates_every_bounded_series_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """increase() drops a series' first sample: series that exist from startup at 0 make the
    first FAILED run, cloud charge or reaper cancel after a restart visible to the alerts."""
    telemetry = Telemetry(monkeypatch)
    metrics.prime()
    runs = telemetry.points("p2p_runs_total")
    assert runs[(("status", "FAILED"),)] == 0 and runs[(("status", "COMPLETED"),)] == 0
    assert telemetry.points("p2p_cost_usd_total") == {
        (("kind", "inference"),): 0,
        (("kind", "cloud"),): 0,
    }
    actions = telemetry.points("p2p_reaper_actions_total")
    assert actions[(("action", "job_cancelled_overdue"),)] == 0
    assert actions[(("action", "error"),)] == 0
    assert telemetry.points("p2p_runs_active") == {(): 0}


def test_every_reaper_action_is_primed() -> None:
    source = (ROOT / "app" / "orchestrator" / "reaper.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"((?:job|bucket|ledger|gateway)_[a-z_]+|error)"', source))
    assert used and used <= set(metrics.REAPER_ACTIONS)


def test_the_oldest_running_job_per_kind_is_a_gauge(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = Telemetry(monkeypatch)
    metrics.jobs_running({"exec": 3000.0})
    assert telemetry.points("p2p_job_oldest_running_seconds") == {
        (("kind", "exec"),): 3000.0,
        (("kind", "prov"),): 0.0,
        (("kind", "smoke"),): 0.0,
        (("kind", "other"),): 0.0,
    }
