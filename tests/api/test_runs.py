import hashlib

import httpx
import pytest

from app.api.deps import SESSION_COOKIE
from app.api.main import AppState
from app.domain.models import RunStatus, StepName
from app.storage.store import Store
from tests.api.conftest import RecordingSubmit, login


async def _create(client: httpx.AsyncClient, arxiv_id: str = "2106.09685") -> httpx.Response:
    return await client.post("/api/runs", json={"arxiv_id": arxiv_id, "claim_hint": None})


async def test_session_sets_a_hardened_cookie(client: httpx.AsyncClient) -> None:
    response = await login(client)
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "max-age=604800", "p2p_session="):
        assert flag in cookie


async def test_unknown_invite_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await login(client, "wrong")).status_code == 401


async def test_create_run_submits_and_returns_ids(
    client: httpx.AsyncClient, submitted: RecordingSubmit, store: Store
) -> None:
    await login(client)
    response = await _create(client)
    assert response.status_code == 202
    body = response.json()
    assert body["run_id"].startswith("r_")
    assert body["events_url"] == f"/api/runs/{body['run_id']}/events"
    assert submitted.run_ids == [body["run_id"]]
    run = await store.get_run(body["run_id"])
    assert run is not None and run.arxiv_id == "2106.09685"


@pytest.mark.parametrize(
    "arxiv_id", ["2106.096", "https://arxiv.org/abs/2106.09685", "2106.09685v", "../etc", ""]
)
async def test_invalid_arxiv_ids_are_400(client: httpx.AsyncClient, arxiv_id: str) -> None:
    await login(client)
    assert (await _create(client, arxiv_id)).status_code == 400


async def test_versioned_ids_are_accepted(client: httpx.AsyncClient) -> None:
    await login(client)
    assert (await _create(client, "2106.09685v2")).status_code == 202


async def test_without_session_is_401(client: httpx.AsyncClient) -> None:
    assert (await _create(client)).status_code == 401


async def test_forged_cookie_is_401(client: httpx.AsyncClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged.value.sig", domain="testserver")
    assert (await _create(client)).status_code == 401


async def test_kill_switch_is_402(client: httpx.AsyncClient, state: AppState) -> None:
    await login(client)
    await state.budget.set_kill_switch(True)
    assert (await _create(client)).status_code == 402


async def test_draining_is_503_with_retry_after(client: httpx.AsyncClient, state: AppState) -> None:
    await login(client)
    state.draining = True
    response = await _create(client)
    assert response.status_code == 503
    assert response.headers["retry-after"].isdigit()
    # deploy/dr_drill.sh tells the drain from other 503s by this body (DRAIN_BODY in its tests)
    assert response.json() == {"error": "deploy in progress"}


async def test_invite_quota_is_429(client: httpx.AsyncClient, store: Store) -> None:
    await store.create_invite(hashlib.sha256(b"one-a-day").hexdigest(), 1, None)
    await login(client, "one-a-day")
    assert (await _create(client)).status_code == 202
    assert (await _create(client)).status_code == 429


async def test_owner_sees_the_run_and_others_get_404(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    await login(client)
    await login(other_client)
    run_id = (await _create(client)).json()["run_id"]
    mine = await client.get(f"/api/runs/{run_id}")
    assert mine.status_code == 200
    assert mine.json()["status"] == "QUEUED"
    assert (await other_client.get(f"/api/runs/{run_id}")).status_code == 404
    assert (await other_client.get(f"/api/runs/{run_id}/events.json")).status_code == 404
    assert (await other_client.post(f"/api/runs/{run_id}/cancel")).status_code == 404
    unknown = "r_" + "0" * 26
    assert (await client.get(f"/api/runs/{unknown}")).status_code == 404


async def test_gallery_runs_are_public(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient, store: Store
) -> None:
    await login(client)
    run_id = (await _create(client)).json()["run_id"]
    await store.set_listed(run_id, True)
    assert (await other_client.get(f"/api/runs/{run_id}")).status_code == 200
    gallery = (await other_client.get("/api/gallery")).json()
    assert [card["id"] for card in gallery] == [run_id]


async def test_cancel(client: httpx.AsyncClient, store: Store) -> None:
    await login(client)
    run_id = (await _create(client)).json()["run_id"]
    assert (await client.post(f"/api/runs/{run_id}/cancel")).status_code == 202
    run = await store.get_run(run_id)
    assert run is not None and run.status is RunStatus.CANCELLED


async def test_approve_requires_awaiting_approval(client: httpx.AsyncClient, store: Store) -> None:
    await login(client)
    run_id = (await _create(client)).json()["run_id"]
    assert (await client.post(f"/api/runs/{run_id}/approve")).status_code == 409
    await store.begin_run(run_id, StepName.AWAITING_APPROVAL, 1)
    assert (await client.post(f"/api/runs/{run_id}/approve")).status_code == 204
    run = await store.get_run(run_id)
    assert run is not None and run.approval == "approved"
    # A decision is final: no flip-flopping between approve and reject.
    assert (await client.post(f"/api/runs/{run_id}/reject")).status_code == 409
    run = await store.get_run(run_id)
    assert run is not None and run.approval == "approved"


async def test_approve_on_a_cancelled_run_is_409(client: httpx.AsyncClient, store: Store) -> None:
    await login(client)
    run_id = (await _create(client)).json()["run_id"]
    await store.begin_run(run_id, StepName.AWAITING_APPROVAL, 1)
    await store.cancel_run(run_id)  # step row still RUNNING, run CANCELLED
    assert (await client.post(f"/api/runs/{run_id}/approve")).status_code == 409


async def test_a_new_approval_step_clears_the_previous_decision(store: Store) -> None:
    run_id = "r_" + "1" * 26
    await store.create_run(run_id, "2106.09685", created_by=None, claim_hint=None, budget={})
    await store.begin_run(run_id, StepName.AWAITING_APPROVAL, 1)
    assert await store.decide_approval(run_id, "approved")
    await store.transition(
        run_id, StepName.AWAITING_APPROVAL, 1, "SUCCEEDED", next_step=(StepName.PROVISIONING, 1)
    )
    await store.transition(
        run_id, StepName.PROVISIONING, 1, "SUCCEEDED", next_step=(StepName.AWAITING_APPROVAL, 2)
    )
    run = await store.get_run(run_id)
    assert run is not None and run.approval is None


async def test_bundle_is_404_until_built(client: httpx.AsyncClient) -> None:
    await login(client)
    run_id = (await _create(client)).json()["run_id"]
    assert (await client.get(f"/api/runs/{run_id}/bundle")).status_code == 404


async def test_session_endpoint_is_rate_limited(client: httpx.AsyncClient) -> None:
    codes = [(await login(client, "wrong")).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


async def test_errors_do_not_leak_internals(client: httpx.AsyncClient) -> None:
    await login(client)
    response = await client.post(
        "/api/runs", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code in (400, 422)
    assert "Traceback" not in response.text


async def test_cross_origin_posts_are_rejected(client: httpx.AsyncClient) -> None:
    await login(client)
    response = await client.post(
        "/api/runs", json={"arxiv_id": "2106.09685"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    same = await client.post(
        "/api/runs", json={"arxiv_id": "2106.09685"}, headers={"Origin": "https://testserver"}
    )
    assert same.status_code == 202


async def test_session_cookie_uses_the_host_prefix(client: httpx.AsyncClient) -> None:
    response = await login(client)
    assert response.headers["set-cookie"].startswith("__Host-p2p_session=")


async def test_oversized_bodies_are_413(client: httpx.AsyncClient) -> None:
    await login(client)
    body = b"{" + b" " * 70_000 + b"}"
    response = await client.post(
        "/api/runs", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


async def test_step_views_hide_storage_keys(client: httpx.AsyncClient, store: Store) -> None:
    await login(client)
    run_id = (await _create(client)).json()["run_id"]
    await store.begin_run(run_id, StepName.INGESTING, 1)
    await store.transition(
        run_id,
        StepName.INGESTING,
        1,
        "SUCCEEDED",
        output_ref="runs/x/ingested.json",
        next_step=(StepName.EXTRACTING_CLAIMS, 1),
    )
    body = (await client.get(f"/api/runs/{run_id}")).json()
    assert "output_ref" not in body["steps"][0]
    assert "runs/x" not in str(body)


async def test_concurrent_invite_use_never_exceeds_the_quota(store: Store) -> None:
    import asyncio
    from datetime import UTC, datetime

    digest = hashlib.sha256(b"burst").hexdigest()
    await store.create_invite(digest, 3, None)
    now = datetime.now(UTC)
    results = await asyncio.gather(*(store.consume_invite(digest, now) for _ in range(10)))
    assert sum(results) == 3


ARABIC_INDIC = "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in "2410.01234")
FULLWIDTH = "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in "2410.01234")


@pytest.mark.parametrize("arxiv_id", [ARABIC_INDIC, FULLWIDTH])
async def test_non_ascii_digits_are_not_an_arxiv_id(
    client: httpx.AsyncClient, arxiv_id: str
) -> None:
    await login(client)
    response = await client.post("/api/runs", json={"arxiv_id": arxiv_id})
    assert response.status_code in (400, 422)
