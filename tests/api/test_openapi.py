"""The OpenAPI schema is the web client's type source (`npm run gen:types`)."""

import json
from pathlib import Path

from app.api.openapi_export import build

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "web" / "openapi.json"


def test_events_json_exposes_typed_envelopes() -> None:
    schema = json.loads(build())
    response = schema["paths"]["/api/runs/{run_id}/events.json"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EventsPage"
    }
    components = schema["components"]["schemas"]
    for name in ("EventEnvelope", "PlanReadyPayload", "Plan", "VerdictReadyPayload", "Verdict"):
        assert name in components


def test_committed_web_schema_is_fresh() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == build(), "run: cd web && npm run gen:types"
