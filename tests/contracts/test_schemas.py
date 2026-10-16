"""Contract tests: hand-written JSON Schemas and pydantic models must agree in both directions."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.domain.contracts import ExecManifest, Results

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
FIXTURES = ROOT / "tests" / "fixtures" / "contracts"


def _schema(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _schema_ok(validator: Draft202012Validator, data: dict[str, Any]) -> bool:
    return not list(validator.iter_errors(data))


def _model_ok(model: type[Results] | type[ExecManifest], data: dict[str, Any]) -> bool:
    try:
        model.model_validate(data)
    except ValidationError:
        return False
    return True


# --- results.json ------------------------------------------------------------------------


def test_results_fixture_valid_for_schema_and_model() -> None:
    data = _fixture("results_valid.json")
    assert _schema_ok(_schema("results.schema.json"), data)
    results = Results.model_validate(data)
    assert [s.seed for s in results.per_seed] == [0, 1, 2]
    assert results.per_seed[2].value is None


RESULTS_MUTATIONS: list[tuple[str, Any]] = [
    ("extra top-level key", lambda d: d.update(verdict="REPRODUCED")),
    ("extra per-seed key", lambda d: d["per_seed"][0].update(note="x")),
    ("null value with ok true", lambda d: d["per_seed"][0].update(value=None)),
    ("null value without ok", lambda d: d["per_seed"][2].pop("ok")),
    ("bad run id", lambda d: d.update(run_id="r_UPPER")),
    ("unknown exit reason", lambda d: d.update(exit_reason="oom")),
    ("baseline without seeds", lambda d: d["baseline"].update(per_seed=[0.9, 0.9])),
    ("too many seeds", lambda d: d.update(per_seed=[{"seed": i, "value": 1.0} for i in range(21)])),
]


@pytest.mark.parametrize(
    ("label", "mutate"), RESULTS_MUTATIONS, ids=[m[0] for m in RESULTS_MUTATIONS]
)
def test_results_mutation_fails_both(label: str, mutate: Any) -> None:
    data = copy.deepcopy(_fixture("results_valid.json"))
    mutate(data)
    assert not _schema_ok(_schema("results.schema.json"), data), f"schema accepted: {label}"
    assert not _model_ok(Results, data), f"model accepted: {label}"


# --- manifest.json -----------------------------------------------------------------------


def test_manifest_fixture_valid_for_schema_and_model() -> None:
    data = _fixture("manifest_valid.json")
    assert _schema_ok(_schema("manifest.schema.json"), data)
    assert ExecManifest.model_validate(data).baseline_name == "resnet18"


MANIFEST_MUTATIONS: list[tuple[str, Any]] = [
    ("entrypoint outside workspace", lambda d: d.update(entrypoint="../etc/passwd")),
    ("entrypoint traversal", lambda d: d.update(entrypoint="workspace/../x.py")),
    ("entrypoint dotdot segment", lambda d: d.update(entrypoint="workspace/a/../../b.py")),
    ("budget over 30 min", lambda d: d.update(budget_seconds=5000)),
    ("relative without baseline", lambda d: d.update(baseline_name=None)),
    ("image by tag", lambda d: d.update(image_digest="latest")),
    ("duplicate seeds", lambda d: d.update(seeds=[1, 1])),
    ("extra key", lambda d: d.update(api_key="x")),
    ("missing higher_is_better", lambda d: d.pop("higher_is_better")),
    ("archives missing", lambda d: d.pop("archives")),
    ("overlay digest not hex", lambda d: d["archives"].update(overlay="x")),
    ("repo digest not hex", lambda d: d["archives"].update(repo="sha256:abc")),
    (
        "data archive traversal",
        lambda d: d["archives"]["data"].append({"name": "..", "sha256": "c" * 64}),
    ),
    (
        "too many data archives",
        lambda d: d["archives"].update(
            data=[{"name": f"d{i}", "sha256": "c" * 64} for i in range(4)]
        ),
    ),
    ("args set the seed", lambda d: d.update(args=["--seed", "3"])),
    ("workspace digest missing", lambda d: d.pop("workspace_sha256")),
    ("workspace digest not hex", lambda d: d.update(workspace_sha256="sha256:" + "f" * 64)),
]


@pytest.mark.parametrize(
    ("label", "mutate"), MANIFEST_MUTATIONS, ids=[m[0] for m in MANIFEST_MUTATIONS]
)
def test_manifest_mutation_fails_both(label: str, mutate: Any) -> None:
    data = copy.deepcopy(_fixture("manifest_valid.json"))
    mutate(data)
    assert not _schema_ok(_schema("manifest.schema.json"), data), f"schema accepted: {label}"
    assert not _model_ok(ExecManifest, data), f"model accepted: {label}"


def test_absolute_manifest_may_omit_baseline() -> None:
    data = copy.deepcopy(_fixture("manifest_valid.json"))
    data.update(claim_type="ABSOLUTE", baseline_name=None)
    assert _schema_ok(_schema("manifest.schema.json"), data)
    assert _model_ok(ExecManifest, data)


# --- events.schema.json ------------------------------------------------------------------


def _export() -> str:
    result = subprocess.run(  # noqa: S603 (our own script, fixed argv)
        [sys.executable, str(ROOT / "scripts" / "export_schemas.py"), "--stdout"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    return result.stdout


def test_export_schemas_is_stable() -> None:
    assert _export() == _export()


def test_committed_events_schema_is_up_to_date() -> None:
    committed = (SCHEMAS / "events.schema.json").read_text(encoding="utf-8")
    assert committed == _export(), "run: uv run python scripts/export_schemas.py"


def test_events_schema_is_valid_and_lists_every_event_type() -> None:
    from app.domain.events import EventType

    schema = json.loads((SCHEMAS / "events.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert set(schema["x-event-types"]) == {t.value for t in EventType}
