"""PLANNING checks every planned Hugging Face dataset the way the provisioner will fetch it:
it exists, is public and ungated, has parquet files for the split and fits its byte cap."""

import hashlib
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from app.discovery import hf_datasets
from app.discovery.hf_datasets import HfDatasets, info_url, split_files
from app.discovery.hf_papers import HF_API
from app.domain.discovery import HF_DATASET_ID
from app.domain.models import DatasetReq
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
CIFAR = "uoft-cs/cifar10"
INFO = f"{HF_API}/datasets/{CIFAR}"


def _provision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "provision", ROOT / "images/provisioner/provision.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["provision"] = module
    spec.loader.exec_module(module)
    return module


def _info(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": CIFAR,
        "private": False,
        "gated": False,
        "disabled": False,
        "siblings": [
            {"rfilename": "README.md", "size": 1_000},
            {"rfilename": "plain_text/train-00000-of-00001.parquet", "size": 120_000_000},
            {"rfilename": "plain_text/test-00000-of-00001.parquet", "size": 24_000_000},
        ],
    }
    return {**data, **overrides}


def _req(split: str | None = "train", max_bytes: int = 2_000_000_000) -> DatasetReq:
    return DatasetReq(kind="hf", id=CIFAR, split=split, max_bytes=max_bytes)


@respx.mock
async def test_a_public_dataset_with_parquet_for_the_split_is_kept(store: Store) -> None:
    route = respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    report = await HfDatasets(store).check([_req("test")])
    assert report.kept == [_req("test")] and report.dropped == [] and report.unchecked == []
    assert route.calls.last.request.url.params["blobs"] == "true"  # sizes, as the provisioner


@respx.mock
async def test_a_made_up_id_is_dropped(store: Store) -> None:
    respx.get(f"{HF_API}/datasets/mstz/wine_quality").mock(
        return_value=httpx.Response(401, json={"error": "Repository not found"})
    )
    req = DatasetReq(kind="hf", id="mstz/wine_quality", split="train")
    report = await HfDatasets(store).check([req])
    assert report.kept == []
    [problem] = report.dropped
    assert problem.id == "mstz/wine_quality" and "not found" in problem.reason


@respx.mock
async def test_gated_and_private_datasets_are_dropped(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info(gated="manual")))
    report = await HfDatasets(store).check([_req()])
    assert report.kept == [] and "gated" in report.dropped[0].reason


@respx.mock
async def test_a_split_without_parquet_files_is_dropped(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    report = await HfDatasets(store).check([_req("validation")])
    assert report.kept == [] and "no parquet files for split validation" in report.dropped[0].reason


@respx.mock
async def test_no_split_means_train_like_the_provisioner(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    assert (await HfDatasets(store).check([_req(None)])).kept == [_req(None)]


@respx.mock
async def test_a_split_over_its_byte_cap_is_dropped(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    report = await HfDatasets(store).check([_req("train", max_bytes=100_000_000)])
    assert report.kept == [] and "over its cap" in report.dropped[0].reason


@respx.mock
async def test_hugging_face_down_keeps_the_dataset_unchecked(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(503))
    report = await HfDatasets(store).check([_req()])
    assert report.kept == [_req()] and report.dropped == [] and report.unchecked == [CIFAR]


@respx.mock
async def test_answers_are_cached_and_a_dataset_is_asked_once(store: Store) -> None:
    route = respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    checker = HfDatasets(store)
    await checker.check([_req("train"), _req("test")])
    await checker.check([_req("train")])
    assert route.call_count == 1


@respx.mock
async def test_a_malformed_sibling_list_drops_the_dataset(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info(siblings="nope")))
    report = await HfDatasets(store).check([_req()])
    assert report.kept == [] and report.dropped


_NAME = st.lists(
    st.sampled_from(["train", "test", "contest", "data", "plain_text", "train-0.parquet",
                     "test.parquet", "x.parquet", "README.md", "test-00000-of-00001.parquet"]),
    min_size=1, max_size=4,
).map("/".join)  # fmt: skip


@given(st.lists(_NAME, max_size=8), st.sampled_from(["train", "test", "validation"]))
def test_split_matching_is_the_provisioners(names: list[str], split: str) -> None:
    assert split_files(names, split) == _provision().split_files(names, split)


@respx.mock
async def test_an_answer_over_the_size_cap_is_unchecked_and_not_cached(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hf_datasets, "MAX_INFO_BYTES", 100)
    route = respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    checker = HfDatasets(store)
    assert (await checker.check([_req()])).unchecked == [CIFAR]
    await checker.check([_req()])
    assert route.call_count == 2  # asked again: an oversize answer is never cached


@respx.mock
async def test_only_the_access_flags_and_parquet_files_are_cached(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    await HfDatasets(store).check([_req()])
    key = hashlib.sha256(info_url(CIFAR).encode()).hexdigest()
    hit = await store.cache_get(hf_datasets.CACHE_PROVIDER, key, max_age=timedelta(hours=1))
    assert hit == {
        "info": {
            "closed": False,
            "files": {
                "plain_text/train-00000-of-00001.parquet": 120_000_000,
                "plain_text/test-00000-of-00001.parquet": 24_000_000,
            },
        }
    }


@respx.mock
async def test_the_summary_runs_where_the_caller_says(store: Store) -> None:
    respx.get(INFO).mock(return_value=httpx.Response(200, json=_info()))
    ran: list[str] = []

    async def run_cpu(fn: Any, body: bytes) -> Any:
        ran.append(fn.__name__)
        return fn(body)

    assert (await HfDatasets(store).check([_req()], run_cpu)).kept == [_req()]
    assert ran == ["summarize"]  # the process pool in production (invariant 12)


@given(st.from_regex(HF_DATASET_ID, fullmatch=True))
def test_a_dataset_id_never_changes_the_host_or_the_path(dataset_id: str) -> None:
    url = httpx.URL(info_url(dataset_id))
    assert (url.scheme, url.host) == ("https", "huggingface.co")
    assert url.path == f"/api/datasets/{dataset_id}" and url.params["blobs"] == "true"
