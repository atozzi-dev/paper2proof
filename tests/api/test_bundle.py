"""GET /api/runs/{id}/bundle: a presigned redirect, or the archive from the local dev store."""

from pathlib import Path

import httpx

from app.api.main import AppState
from app.bundle.store import ArtifactBundleStore
from app.storage.artifacts import LocalArtifactStore
from tests.api.conftest import login


class PresignedStore(ArtifactBundleStore):
    async def presigned_url(self, run_id: str, ttl_s: int) -> str | None:
        return f"https://bundles.example/{run_id}?ttl={ttl_s}"


async def _owned_run(client: httpx.AsyncClient) -> str:
    await login(client)
    response = await client.post("/api/runs", json={"arxiv_id": "2106.09685", "claim_hint": None})
    run_id: str = response.json()["run_id"]
    return run_id


async def test_bundle_is_served_from_the_local_store(
    client: httpx.AsyncClient, state: AppState, tmp_path: Path
) -> None:
    state.bundles = ArtifactBundleStore(LocalArtifactStore(tmp_path))
    run_id = await _owned_run(client)
    assert (await client.get(f"/api/runs/{run_id}/bundle")).status_code == 404
    await state.bundles.put(run_id, b"\x1f\x8bbundle")
    response = await client.get(f"/api/runs/{run_id}/bundle")
    assert response.status_code == 200
    assert response.content == b"\x1f\x8bbundle"
    assert response.headers["content-type"] == "application/gzip"
    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="proof-bundle-{run_id}.tar.gz"'


async def test_bundle_redirects_to_a_short_presigned_url(
    client: httpx.AsyncClient, state: AppState, tmp_path: Path
) -> None:
    state.bundles = PresignedStore(LocalArtifactStore(tmp_path))
    run_id = await _owned_run(client)
    await state.bundles.put(run_id, b"x")
    response = await client.get(f"/api/runs/{run_id}/bundle")
    assert response.status_code == 302
    assert response.headers["location"] == f"https://bundles.example/{run_id}?ttl=300"


async def test_bundle_of_someone_elses_run_is_404(
    client: httpx.AsyncClient, state: AppState, tmp_path: Path
) -> None:
    state.bundles = ArtifactBundleStore(LocalArtifactStore(tmp_path))
    run_id = await _owned_run(client)
    await state.bundles.put(run_id, b"x")
    client.cookies.clear()
    assert (await client.get(f"/api/runs/{run_id}/bundle")).status_code == 404


async def test_a_gallery_bundle_is_public_with_security_headers(
    client: httpx.AsyncClient, state: AppState, tmp_path: Path
) -> None:
    state.bundles = ArtifactBundleStore(LocalArtifactStore(tmp_path))
    run_id = await _owned_run(client)
    await state.bundles.put(run_id, b"x")
    assert await state.store.set_listed(run_id, True)
    client.cookies.clear()
    response = await client.get(f"/api/runs/{run_id}/bundle")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "content-security-policy" in response.headers


async def test_a_presigned_bundle_that_does_not_exist_is_404(
    client: httpx.AsyncClient, state: AppState, tmp_path: Path
) -> None:
    state.bundles = PresignedStore(LocalArtifactStore(tmp_path))
    run_id = await _owned_run(client)
    assert (await client.get(f"/api/runs/{run_id}/bundle")).status_code == 404
