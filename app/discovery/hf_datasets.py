"""Whether a planned Hugging Face dataset can be fetched: it exists, is public and ungated (jobs
never receive tokens), has parquet files for its split and fits its byte cap. These are the
rules of images/provisioner/provision.py::hf_fetch, checked at PLANNING so that a dataset id
the Planner made up is caught before any provisioning or GPU work."""

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from app.discovery.hf_papers import CACHE_TTL, HF_API, HfUnavailable
from app.domain.models import DatasetReq
from app.ingestion.arxiv import USER_AGENT
from app.storage.store import Store

DEFAULT_SPLIT = "train"  # what the provisioner fetches when the plan names no split
MAX_INFO_BYTES = 8 * 2**20  # a dataset with tens of thousands of files: treated as unchecked
CACHE_PROVIDER = "hf_dataset"

Info = dict[str, Any]
RunCpu = Callable[[Callable[[bytes], Info], bytes], Awaitable[Info]]


def split_files(names: Sequence[str], split: str) -> list[str]:
    """Parquet files of a split: a path segment named after it, or a file named
    `<split>.parquet` / `<split>-*.parquet` (the provisioner's rule, kept equal by a test)."""
    wanted = []
    for name in names:
        parts = name.split("/")
        if not name.endswith(".parquet"):
            continue
        file = parts[-1]
        if split in parts[:-1] or file == f"{split}.parquet" or file.startswith(f"{split}-"):
            wanted.append(name)
    return wanted


def info_url(dataset_id: str) -> str:
    """The ids already match HF_DATASET_ID (correct_plan); quoted all the same."""
    return f"{HF_API}/datasets/{quote(dataset_id, safe='/')}?blobs=true"


def summarize(body: bytes) -> Info:
    """What the check needs from a dataset info answer, and all that is cached: the access
    flags and the parquet files with their sizes (CPU-bound: runs in the process pool)."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise HfUnavailable("Hugging Face returned a malformed body") from None
    if not isinstance(data, dict):
        raise HfUnavailable("Hugging Face returned a malformed body")
    siblings = data.get("siblings")
    files = (
        {
            s["rfilename"]: s["size"] if isinstance(s.get("size"), int) else 0
            for s in siblings
            if isinstance(s, dict)
            and isinstance(s.get("rfilename"), str)
            and s["rfilename"].endswith(".parquet")
        }
        if isinstance(siblings, list)
        else None
    )
    return {
        "closed": bool(data.get("private") or data.get("gated") or data.get("disabled")),
        "files": files,
    }


class DatasetProblem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    split: str | None
    reason: str  # written by this module, never by a model


class DatasetReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    kept: list[DatasetReq]
    dropped: list[DatasetProblem]
    unchecked: list[str]  # Hugging Face did not answer: kept, the provisioner checks again


def _problem(info: Info | None, req: DatasetReq) -> str | None:
    if info is None:
        return "not found on Hugging Face (or private)"
    if info["closed"]:
        return "gated, private or disabled: jobs never receive Hugging Face tokens"
    files = info["files"]
    if not isinstance(files, dict):
        return "Hugging Face did not list its files"
    split = req.split or DEFAULT_SPLIT
    wanted = split_files(list(files), split)
    if not wanted:
        return f"has no parquet files for split {split}"
    total = sum(int(files[n]) for n in wanted)
    if total > req.max_bytes:
        return f"split {split} is {total} bytes, over its cap of {req.max_bytes}"
    return None


async def _inline(fn: Callable[[bytes], Info], body: bytes) -> Info:
    return fn(body)


class HfDatasets:
    def __init__(self, store: Store, http: httpx.AsyncClient | None = None) -> None:
        self._store = store
        self._http = http

    async def _fetch(self, url: str, run_cpu: RunCpu) -> Info | None:
        """GET with a byte cap; None for any non-200 but an outage (HF answers 401 for a
        repository that does not exist). An outage raises HfUnavailable, never cached."""
        client = self._http or httpx.AsyncClient(timeout=30)
        try:
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise HfUnavailable(f"Hugging Face returned HTTP {response.status_code}")
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_INFO_BYTES:
                        raise HfUnavailable("Hugging Face answer over the size cap")
        except httpx.HTTPError as err:
            raise HfUnavailable(f"Hugging Face unreachable: {type(err).__name__}") from None
        finally:
            if self._http is None:
                await client.aclose()
        return await run_cpu(summarize, bytes(body))

    async def _info(self, dataset_id: str, run_cpu: RunCpu) -> Info | None:
        url = info_url(dataset_id)
        key = hashlib.sha256(url.encode()).hexdigest()
        hit = await self._store.cache_get(CACHE_PROVIDER, key, max_age=CACHE_TTL)
        if hit is not None:
            cached: Info | None = hit.get("info")
            return cached
        info = await self._fetch(url, run_cpu)
        await self._store.cache_put(CACHE_PROVIDER, key, {"info": info})
        return info

    async def check(
        self, datasets: list[DatasetReq], run_cpu: RunCpu | None = None
    ) -> DatasetReport:
        kept: list[DatasetReq] = []
        dropped: list[DatasetProblem] = []
        unchecked: list[str] = []
        for req in datasets:
            try:
                info = await self._info(req.id, run_cpu or _inline)
            except HfUnavailable:
                kept.append(req)
                unchecked.append(req.id)
                continue
            problem = _problem(info, req)
            if problem is None:
                kept.append(req)
            else:
                dropped.append(DatasetProblem(id=req.id, split=req.split, reason=problem))
        return DatasetReport(kept=kept, dropped=dropped, unchecked=unchecked)
