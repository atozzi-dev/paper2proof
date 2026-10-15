from unittest import mock

import httpx

from app.api.main import AppState
from app.storage.store import Store
from tests.api.conftest import ADMIN, login

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


async def test_gallery_curation(client: httpx.AsyncClient, store: Store) -> None:
    await login(client)
    run_id = (await client.post("/api/runs", json={"arxiv_id": "2106.09685"})).json()["run_id"]
    response = await client.post(f"/admin/gallery/{run_id}", json={"listed": True}, headers=HEAD)
    assert response.status_code == 204
    run = await store.get_run(run_id)
    assert run is not None and run.listed
    missing = await client.post("/admin/gallery/r_" + "0" * 26, json={"listed": True}, headers=HEAD)
    assert missing.status_code == 404


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
