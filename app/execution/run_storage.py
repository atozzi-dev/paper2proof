"""Access to the per-run bucket (`p2p-run-<slug>`) from the control plane.

The S3 implementation (sa-job-launcher access key) lands with the Nebius backend (A-20).
Everything read back from the bucket was written next to untrusted code: callers validate it.
"""

from typing import Protocol

MAX_READ_BYTES = 1024 * 1024


class RunStorage(Protocol):
    """`get_bytes` returns None for a missing object and raises ValueError for one larger than
    `max_bytes` without buffering it (check the size first, then read at most max_bytes + 1):
    every object here may have been planted by untrusted code."""

    async def get_bytes(
        self, run_id: str, key: str, *, max_bytes: int = MAX_READ_BYTES
    ) -> bytes | None: ...
    async def put_bytes(self, run_id: str, key: str, data: bytes) -> None: ...
    async def delete(self, run_id: str, key: str) -> None: ...


class FakeRunStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def get_bytes(
        self, run_id: str, key: str, *, max_bytes: int = MAX_READ_BYTES
    ) -> bytes | None:
        data = self.objects.get((run_id, key))
        if data is not None and len(data) > max_bytes:
            raise ValueError(f"{key} is larger than {max_bytes} bytes")
        return data

    async def put_bytes(self, run_id: str, key: str, data: bytes) -> None:
        self.objects[(run_id, key)] = data

    async def delete(self, run_id: str, key: str) -> None:
        self.objects.pop((run_id, key), None)
