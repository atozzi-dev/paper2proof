"""Where built bundles live: `bundles/<run_id>/proof-bundle.tar.gz` in `p2p-bundles` (prod) or
the local artifact directory (test/dev). Downloads go through short presigned URLs when the
store can make them (architecture §10.3)."""

import asyncio
from typing import Protocol

from app.storage.artifacts import ArtifactStore

BUNDLE_NAME = "proof-bundle.tar.gz"
PRESIGN_TTL_S = 300


def bundle_key(run_id: str) -> str:
    return f"bundles/{run_id}/{BUNDLE_NAME}"


def download_name(run_id: str) -> str:
    return f"proof-bundle-{run_id}.tar.gz"


class BundleStore(Protocol):
    async def put(self, run_id: str, data: bytes) -> None: ...
    async def get(self, run_id: str) -> bytes | None: ...
    async def exists(self, run_id: str) -> bool: ...
    async def presigned_url(self, run_id: str, ttl_s: int) -> str | None:
        """A short-lived download URL, or None when this store can't make one."""
        ...


class ArtifactBundleStore:
    """Bundles in an ArtifactStore. A local store can't presign: the API serves the bytes."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def put(self, run_id: str, data: bytes) -> None:
        await asyncio.to_thread(self._artifacts.put_bytes, bundle_key(run_id), data)

    async def get(self, run_id: str) -> bytes | None:
        try:
            return await asyncio.to_thread(self._artifacts.get_bytes, bundle_key(run_id))
        except FileNotFoundError:
            return None

    async def exists(self, run_id: str) -> bool:
        return await self.get(run_id) is not None

    async def presigned_url(self, run_id: str, ttl_s: int) -> str | None:
        return None
