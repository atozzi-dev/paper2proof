import hashlib
import json
from unittest import mock

import httpx

from app.api.main import AppState
from app.api.openapi_export import build
from app.domain.models import ReasonCode, RunStatus, Scale, Verdict, VerdictKind
from app.storage.store import Store
from tests.api.conftest import ADMIN, login

COMPLETED = RunStatus.COMPLETED

HEAD = {"X-Admin-Token": ADMIN}


async def test_wrong_token_is_rejected_in_constant_time(client: httpx.AsyncClient) -> None:
    with mock.patch(
        "app.api.admin.hmac.compare_digest", wraps=__import__("hmac").compare_digest
    ) as cmp:
        response = await client.post(
            "/admin/drain", json={"on": True}, headers={"X-Admin-Token": "x"}
        )
    assert response.status_code == 401
    assert cmp.called
    assert (await client.post("/admin/drain", json={"on": True})).status_code == 401


async def test_drain_and_kill_switch(client: httpx.AsyncClient, state: AppState) -> None:
    assert (await client.post("/admin/drain", json={"on": True}, headers=HEAD)).status_code == 204
    assert state.draining
    assert (
        await client.post("/admin/kill-switch", json={"on": True}, headers=HEAD)
    ).status_code == 204
    assert await state.budget.kill_switch_active()


def _verdict(kind: VerdictKind) -> Verdict:
    reason = ReasonCode.HIGH_VARIANCE if kind is VerdictKind.INCONCLUSIVE else None
    return Verdict(
        kind=kind,
        reason=reason,
        n_seeds=3,
        mean=0.9,
        std=0.01,
        ci_low=0.88,
        ci_high=0.92,
        tolerance={"w": 0.01, "v": 0.9},
    )


async def _finished_run(
    client: httpx.AsyncClient, store: Store, *kinds: VerdictKind, status: RunStatus = COMPLETED
) -> str:
    await login(client)
    created = await client.post("/api/runs", json={"arxiv_id": "2106.09685"})
    run_id: str = created.json()["run_id"]
    for index, kind in enumerate(kinds):
        await store.save_verdict(run_id, f"c{index}", _verdict(kind), Scale.FULL, "abc", "e")
    await store.set_run_status(run_id, status)
    return run_id


async def _curate(client: httpx.AsyncClient, run_id: str, **body: bool) -> httpx.Response:
    return await client.post(f"/admin/gallery/{run_id}", json=body, headers=HEAD)


async def test_gallery_curation(client: httpx.AsyncClient, store: Store) -> None:
    run_id = await _finished_run(client, store, VerdictKind.REPRODUCED)
    assert (await _curate(client, run_id, listed=True)).status_code == 204
    run = await store.get_run(run_id)
    assert run is not None and run.listed
    assert run.verdict_summary == "1 REPRODUCED"  # the same line VERDICT writes
    cards = (await client.get("/api/gallery")).json()
    assert [(c["id"], c["verdict_summary"]) for c in cards] == [(run_id, "1 REPRODUCED")]
    missing = await _curate(client, "r_" + "0" * 26, listed=True)
    assert missing.status_code == 404


async def test_a_run_still_in_progress_is_never_listed(
    client: httpx.AsyncClient, store: Store
) -> None:
    await login(client)
    run_id = (await client.post("/api/runs", json={"arxiv_id": "2106.09685"})).json()["run_id"]
    assert (await _curate(client, run_id, listed=True, reviewed=True)).status_code == 409
    run = await store.get_run(run_id)
    assert run is not None and not run.listed


async def test_a_negative_verdict_is_listed_only_after_review(
    client: httpx.AsyncClient, store: Store
) -> None:
    """Architecture 16.1: no NOT_REPRODUCED run reaches the gallery without a human review."""
    run_id = await _finished_run(
        client, store, VerdictKind.INCONCLUSIVE, VerdictKind.NOT_REPRODUCED
    )
    refused = await _curate(client, run_id, listed=True)
    assert refused.status_code == 409 and "reviewed" in refused.json()["error"]
    assert (await client.get("/api/gallery")).json() == []
    assert (await _curate(client, run_id, listed=True, reviewed=True)).status_code == 204
    run = await store.get_run(run_id)
    assert run is not None and run.listed
    assert run.verdict_summary == "1 NOT_REPRODUCED · 1 INCONCLUSIVE"


async def test_unlisting_always_works(client: httpx.AsyncClient, store: Store) -> None:
    """Takedown: a listed run leaves the gallery whatever its state."""
    run_id = await _finished_run(client, store, VerdictKind.NOT_REPRODUCED)
    assert (await _curate(client, run_id, listed=True, reviewed=True)).status_code == 204
    await store.set_run_status(run_id, RunStatus.RUNNING)  # whatever happens later
    assert (await _curate(client, run_id, listed=False)).status_code == 204
    run = await store.get_run(run_id)
    assert run is not None and not run.listed
    assert (await client.get("/api/gallery")).json() == []


async def test_admin_lists_runs_to_curate(client: httpx.AsyncClient, store: Store) -> None:
    listed = await _finished_run(client, store, VerdictKind.REPRODUCED)
    assert (await _curate(client, listed, listed=True)).status_code == 204
    candidate = await _finished_run(client, store, VerdictKind.NOT_REPRODUCED)
    everything = (await client.get("/admin/runs", headers=HEAD)).json()
    assert [r["id"] for r in everything] == [candidate, listed]  # newest first
    unlisted = (await client.get("/admin/runs?listed=false", headers=HEAD)).json()
    assert unlisted == [
        {
            "id": candidate,
            "arxiv_id": "2106.09685",
            "status": "COMPLETED",
            "listed": False,
            "created_at": unlisted[0]["created_at"],
            "verdicts": ["NOT_REPRODUCED"],
        }
    ]
    assert (await client.get("/admin/runs")).status_code == 401
    assert (await client.get("/admin/runs?limit=0", headers=HEAD)).status_code == 400


async def test_every_new_admin_route_needs_the_token(client: httpx.AsyncClient) -> None:
    run_id = "r_" + "0" * 26
    calls = [
        ("GET", "/admin/runs", None),
        ("GET", "/admin/invites", None),
        ("POST", "/admin/invites", {}),
        ("POST", "/admin/invites/revoke", {"id": "ab" * 6}),
        ("POST", f"/admin/gallery/{run_id}", {"listed": False}),
    ]
    for method, path, body in calls:
        response = await client.request(method, path, json=body, headers={"X-Admin-Token": "x"})
        assert response.status_code == 401, path


async def test_a_malformed_run_id_is_not_found(client: httpx.AsyncClient) -> None:
    assert (await _curate(client, "not-a-run", listed=False)).status_code == 404


async def test_admin_routes_are_not_in_the_public_schema() -> None:
    paths = json.loads(build())["paths"]
    assert paths and not [p for p in paths if p.startswith("/admin")]


async def test_invite_creation_returns_the_code_once(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    response = await client.post(
        "/admin/invites",
        json={"quota_per_day": 2, "expires_at": "2026-12-15T00:00:00Z"},
        headers=HEAD,
    )
    assert response.status_code == 201
    code = response.json()["code"]
    assert len(code) >= 16
    assert (await login(other_client, code)).status_code == 204


async def test_invites_are_listed_without_their_codes_and_can_be_revoked(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    code = (await client.post("/admin/invites", json={}, headers=HEAD)).json()["code"]
    digest = hashlib.sha256(code.encode()).hexdigest()
    session = other_client
    assert (await login(session, code)).status_code == 204
    listing = (await client.get("/admin/invites", headers=HEAD)).json()
    assert code not in json.dumps(listing)
    mine = [i for i in listing if i["id"] == digest[:12]]
    assert mine == [
        {
            "id": digest[:12],
            "quota_per_day": 3,  # the default quota
            "used_today": 0,
            "used_on": None,
            "expires_at": None,
            "active": True,
        }
    ]
    revoked = await client.post("/admin/invites/revoke", json={"id": digest[:12]}, headers=HEAD)
    assert revoked.status_code == 204
    assert (await login(client, code)).status_code == 401  # no new session
    run = await session.post("/api/runs", json={"arxiv_id": "2106.09685"})
    assert run.status_code == 429  # an existing session gets no new run either
    listing = (await client.get("/admin/invites", headers=HEAD)).json()
    assert [i["active"] for i in listing if i["id"] == digest[:12]] == [False]


async def test_revoking_needs_one_known_invite(client: httpx.AsyncClient, store: Store) -> None:
    await store.create_invite("ab" * 32, 3, None)
    await store.create_invite("ab" * 6 + "cd" * 26, 3, None)
    ambiguous = await client.post("/admin/invites/revoke", json={"id": "ab" * 6}, headers=HEAD)
    assert ambiguous.status_code == 409
    exact = await client.post("/admin/invites/revoke", json={"id": "ab" * 32}, headers=HEAD)
    assert exact.status_code == 204
    unknown = await client.post("/admin/invites/revoke", json={"id": "ef" * 6}, headers=HEAD)
    assert unknown.status_code == 404
    for bad in ("ab" * 5, "zz" * 6, "%" * 12):
        response = await client.post("/admin/invites/revoke", json={"id": bad}, headers=HEAD)
        assert response.status_code == 400, bad


async def test_empty_admin_token_never_authenticates(
    state: AppState, client: httpx.AsyncClient
) -> None:
    from pydantic import SecretStr

    state.settings = state.settings.model_copy(update={"admin_token": SecretStr("")})
    assert (await client.post("/admin/drain", json={"on": True})).status_code == 401
    empty = {"X-Admin-Token": ""}
    assert (await client.post("/admin/drain", json={"on": True}, headers=empty)).status_code == 401


async def test_admin_failures_are_rate_limited(client: httpx.AsyncClient) -> None:
    bad = {"X-Admin-Token": "x"}
    codes = [
        (await client.post("/admin/drain", json={"on": True}, headers=bad)).status_code
        for _ in range(12)
    ]
    assert codes[:10] == [401] * 10
    assert 429 in codes[10:]
