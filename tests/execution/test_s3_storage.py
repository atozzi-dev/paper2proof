"""S3 access to the run buckets, against moto's in-process S3 (no network, no credentials)."""

from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import SecretStr

from app.execution import s3 as s3_module
from app.execution.naming import bucket_name
from app.execution.s3 import S3ArtifactStore, S3RunStorage, empty_bucket, make_s3_client

RUN = "r_01jb7z00000000000000000000"
ENDPOINT = "https://storage.eu-north1.example.test"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPOINT)
    with mock_aws():
        client = make_s3_client(ENDPOINT, "eu-north1", "key-id", SecretStr("key-secret"))
        client.create_bucket(
            Bucket=bucket_name(RUN),
            CreateBucketConfiguration={"LocationConstraint": "eu-north1"},
        )
        yield client


async def test_put_get_delete_round_trip(s3: Any) -> None:
    storage = S3RunStorage(s3)
    await storage.put_bytes(RUN, "staging/provision_report.json", b'{"ok": true}')
    assert await storage.get_bytes(RUN, "staging/provision_report.json") == b'{"ok": true}'
    await storage.delete(RUN, "staging/provision_report.json")
    assert await storage.get_bytes(RUN, "staging/provision_report.json") is None


async def test_missing_object_is_none(s3: Any) -> None:
    assert await S3RunStorage(s3).get_bytes(RUN, "results/1/results.json") is None


async def test_empty_object(s3: Any) -> None:
    storage = S3RunStorage(s3)
    await storage.put_bytes(RUN, "results/1/stderr.log", b"")
    assert await storage.get_bytes(RUN, "results/1/stderr.log") == b""


async def test_delete_of_a_missing_object_is_a_no_op(s3: Any) -> None:
    await S3RunStorage(s3).delete(RUN, "smoke/1/result.json")


async def test_oversized_object_is_refused_without_reading_it(s3: Any) -> None:
    storage = S3RunStorage(s3)
    await storage.put_bytes(RUN, "results/1/stdout.log", b"x" * 2048)
    with pytest.raises(ValueError, match="larger than 1024"):
        await storage.get_bytes(RUN, "results/1/stdout.log", max_bytes=1024)
    assert await storage.get_bytes(RUN, "results/1/stdout.log", max_bytes=2048) == b"x" * 2048


async def test_object_growing_after_the_size_check_is_still_refused(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size from HEAD can be stale (untrusted code may rewrite the object meanwhile): the
    read itself is bounded to max_bytes + 1."""
    storage = S3RunStorage(s3)
    await storage.put_bytes(RUN, "results/1/results.json", b"x" * 2048)
    real_head = s3.head_object

    def stale_head(**kwargs: Any) -> dict[str, Any]:
        response: dict[str, Any] = real_head(**kwargs)
        response["ContentLength"] = 10
        return response

    monkeypatch.setattr(s3, "head_object", stale_head)
    with pytest.raises(ValueError, match="larger than 1024"):
        await storage.get_bytes(RUN, "results/1/results.json", max_bytes=1024)


@pytest.mark.parametrize(
    "key", ["", "/etc/passwd", "../other/x", "results/../../x", "a\\b", "a\x00b", "x" * 1025]
)
async def test_unsafe_keys_are_rejected(s3: Any, key: str) -> None:
    storage = S3RunStorage(s3)
    with pytest.raises(ValueError, match="object key"):
        await storage.get_bytes(RUN, key)
    with pytest.raises(ValueError, match="object key"):
        await storage.put_bytes(RUN, key, b"x")


async def test_run_id_must_be_a_run_id(s3: Any) -> None:
    with pytest.raises(ValueError, match="not a run id"):
        await S3RunStorage(s3).get_bytes("p2p-cache", "overlays/x")


async def test_empty_bucket_removes_every_object_and_upload(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s3_module, "DELETE_BATCH", 2)  # several delete_objects batches
    bucket = bucket_name(RUN)
    for i in range(5):
        s3.put_object(Bucket=bucket, Key=f"data/{i}", Body=b"x")
    s3.create_multipart_upload(Bucket=bucket, Key="staging/overlay.tar.zst")
    assert await empty_bucket(s3, bucket) is True
    assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] == 0
    assert not s3.list_multipart_uploads(Bucket=bucket).get("Uploads")


async def test_empty_bucket_falls_back_to_single_deletes(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys planted by untrusted code may not fit in DeleteObjects' XML body (control
    characters): each key is then deleted on its own (the key travels in the URL)."""
    bucket = bucket_name(RUN)
    for key in ("results/1/a b%20c", "results/1/plain"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")

    def malformed(**kwargs: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "MalformedXML"}}, "DeleteObjects")

    monkeypatch.setattr(s3, "delete_objects", malformed)
    assert await empty_bucket(s3, bucket) is True
    assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] == 0


async def test_throttling_is_not_turned_into_single_deletes(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = bucket_name(RUN)
    s3.put_object(Bucket=bucket, Key="results/1/x", Body=b"x")

    def slow_down(**kwargs: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "SlowDown"}}, "DeleteObjects")

    monkeypatch.setattr(s3, "delete_objects", slow_down)
    with pytest.raises(ClientError):
        await empty_bucket(s3, bucket)
    assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] == 1


async def test_pending_upload_with_an_odd_key_is_aborted(s3: Any) -> None:
    bucket = bucket_name(RUN)
    s3.create_multipart_upload(Bucket=bucket, Key="staging/a b%2F+c")
    assert await empty_bucket(s3, bucket) is True
    assert not s3.list_multipart_uploads(Bucket=bucket).get("Uploads")


async def test_empty_bucket_is_bounded_per_call(s3: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_module, "DELETE_BATCH", 2)
    bucket = bucket_name(RUN)
    for i in range(5):
        s3.put_object(Bucket=bucket, Key=f"data/{i}", Body=b"x")
    assert await empty_bucket(s3, bucket, max_objects=3) is False  # resumes on the next call
    assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] < 5
    assert await empty_bucket(s3, bucket, max_objects=3) is True


async def test_empty_bucket_of_a_missing_bucket_is_a_no_op(s3: Any) -> None:
    assert await empty_bucket(s3, "p2p-run-01jb7z00000000000000000009") is True


async def test_object_shrinking_to_empty_after_the_size_check(
    s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = S3RunStorage(s3)
    await storage.put_bytes(RUN, "results/1/results.json", b"")
    real_head = s3.head_object

    def stale_head(**kwargs: Any) -> dict[str, Any]:
        response: dict[str, Any] = real_head(**kwargs)
        response["ContentLength"] = 10  # the HEAD saw 10 bytes, the GET finds 0 (416)
        return response

    monkeypatch.setattr(s3, "head_object", stale_head)
    assert await storage.get_bytes(RUN, "results/1/results.json") == b""


def test_plain_http_endpoints_are_refused_unless_allowed() -> None:
    with pytest.raises(ValueError, match="https"):
        make_s3_client("http://storage.example.test", "eu-north1", "k", SecretStr("s"))
    client = make_s3_client(
        "http://localhost:8333", "us-east-1", "k", SecretStr("s"), allow_http=True
    )
    assert client.meta.endpoint_url == "http://localhost:8333"


# --- control-plane artifacts ----------------------------------------------------------------


@pytest.fixture
def artifacts(s3: Any) -> S3ArtifactStore:
    s3.create_bucket(
        Bucket="p2p-state", CreateBucketConfiguration={"LocationConstraint": "eu-north1"}
    )
    return S3ArtifactStore(s3, "p2p-state")


def test_artifact_round_trip(artifacts: S3ArtifactStore) -> None:
    artifacts.put_bytes("runs/r_1/paper.json", b"{}")
    assert artifacts.get_bytes("runs/r_1/paper.json") == b"{}"
    artifacts.put_json("runs/r_1/plan.json", {"b": 1, "a": "è"})
    assert artifacts.get_json("runs/r_1/plan.json") == {"a": "è", "b": 1}
    assert artifacts.get_bytes("runs/r_1/plan.json") == '{"a": "è", "b": 1}'.encode()


def test_missing_artifact_raises_file_not_found(artifacts: S3ArtifactStore) -> None:
    with pytest.raises(FileNotFoundError):
        artifacts.get_bytes("runs/r_1/missing.json")


def test_missing_artifact_bucket_is_not_a_missing_key(s3: Any) -> None:
    with pytest.raises(ClientError):
        S3ArtifactStore(s3, "p2p-no-such-bucket").get_bytes("runs/r_1/x.json")


@pytest.mark.parametrize("key", ["", "/abs", "runs/../x", "Runs/UPPER", "a//b"])
def test_artifact_keys_are_checked(artifacts: S3ArtifactStore, key: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact key"):
        artifacts.put_bytes(key, b"x")
    with pytest.raises(ValueError, match="invalid artifact key"):
        artifacts.get_bytes(key)


def test_artifact_store_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPOINT)
    with mock_aws():
        store = S3ArtifactStore.connect(
            endpoint_url=ENDPOINT,
            region="eu-north1",
            bucket="p2p-state",
            key_id="key-id",
            secret=SecretStr("key-secret-value"),
        )
        store._s3.create_bucket(
            Bucket="p2p-state", CreateBucketConfiguration={"LocationConstraint": "eu-north1"}
        )
        store.put_bytes("bundles/r_1/proof-bundle.tar.gz", b"x")
        assert store.get_bytes("bundles/r_1/proof-bundle.tar.gz") == b"x"
        assert "key-secret-value" not in repr(store)
