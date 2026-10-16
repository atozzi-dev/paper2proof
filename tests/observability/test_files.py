"""The dashboard and alert files the human imports only use metrics the app registers."""

import json
import re
from pathlib import Path

import yaml
from opentelemetry.sdk.metrics import MeterProvider

from app.observability.metrics import Instruments
from app.orchestrator.budget import load_budget_limits

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "infra" / "observability"
SUFFIXES = ("_bucket", "_count", "_sum")


def _registered() -> set[str]:
    meter = MeterProvider().get_meter("names")
    return {
        getattr(instrument, "name", "")
        for instrument in vars(Instruments(meter)).values()
        if getattr(instrument, "name", "")
    }


def _used(expression: str) -> set[str]:
    names = set(re.findall(r"\bp2p_[a-z_]+", expression))
    return {next((n[: -len(s)] for s in SUFFIXES if n.endswith(s)), n) for n in names}


def test_the_dashboard_queries_registered_metrics() -> None:
    dashboard = json.loads((FOLDER / "dashboard.json").read_text(encoding="utf-8"))
    expressions = [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    used = set().union(*map(_used, expressions))
    assert used and used <= _registered()
    assert {"p2p_runs_active", "p2p_model_tokens_total", "p2p_gpu_seconds_total"} <= used


def test_the_alerts_query_registered_metrics_and_the_configured_cap() -> None:
    rules = yaml.safe_load((FOLDER / "alerts.yaml").read_text(encoding="utf-8"))
    expressions = [r["expr"] for g in rules["groups"] for r in g["rules"]]
    assert set().union(*map(_used, expressions)) <= _registered()
    cap = load_budget_limits(ROOT / "config" / "budgets.yaml").global_.cloud_usd_per_day
    spend = next(e for e in expressions if 'kind="cloud"' in e)
    assert f"0.80 * {cap:.2f}" in spend
