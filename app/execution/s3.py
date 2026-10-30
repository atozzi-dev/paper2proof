"""S3 access to the per-run buckets with the sa-job-launcher access key.

Nebius Object Storage speaks the S3 API; every boto3 call of the execution plane lives in this
module and runs in a worker thread (boto3 is blocking). Everything read back from a run bucket
was written next to untrusted code: reads are size-bounded and callers validate the content.
Assumptions to re-check with spike S-11 are marked [S-11].
"""

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.execution.naming import bucket_name
from app.execution.run_storage import MAX_READ_BYTES
from app.storage.artifacts import check_key

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client as S3Client
else:
    S3Client = Any

MAX_KEY_LEN = 1024
DELETE_BATCH = 1000  # DeleteObjects limit
MAX_EMPTY = 20_000  # objects deleted per empty_bucket call
_MISSING_KEY = frozenset({"404", "NoSuchKey", "NotFound"})
_MISSING = _MISSING_KEY | {"NoSuchBucket"}
_UNENCODABLE = frozenset({"MalformedXML", "InvalidArgument"})
_UNSAFE_KEY = re.compile(r"[\x00-\x1f\x7f\\]")


def make_s3_client(
    endpoint_url: str, region: str, key_id: str, secret: SecretStr, *, allow_http: bool = False
) -> S3Client:
    """`allow_http` only for the local dev store (SeaweedFS): anywhere else requests and the
    untrusted results they carry go over TLS."""
    if not endpoint_url.startswith("https://") and not (
        allow_http and endpoint_url.startswith("http://")
    ):
        raise ValueError("the S3 endpoint must be an https:// URL")
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},  # [S-11] virtual-hosted style not checked yet
        # boto3 >= 1.36 sends CRC32 checksums by default; S3-compatible stores may refuse them.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 4, "mode": "standard"},
        connect_timeout=10,
        read_timeout=60,
    )
    client: S3Client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret.get_secret_value(),
        config=config,
    )
    return client


def _error_code(err: ClientError) -> str:
    return str(err.response.get("Error", {}).get("Code", ""))


def check_run_key(key: str) -> str:
    parts = key.split("/")
    if (
        not key
        or len(key) > MAX_KEY_LEN
        or key.startswith("/")
        or _UNSAFE_KEY.search(key)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError(f"invalid object key: {key!r}")
    return key


class S3RunStorage:
    """RunStorage over the run's own bucket `p2p-run-<slug>`."""

    def __init__(self, client: S3Client) -> None:
        self._s3 = client

    def _get(self, bucket: str, key: str, max_bytes: int) -> bytes | None:
        try:
            size = self._s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        except ClientError as err:
            if _error_code(err) in _MISSING:
                return None
            raise
        if size > max_bytes:
            raise ValueError(f"{key} is larger than {max_bytes} bytes")
        try:
            if size == 0:
                response = self._s3.get_object(Bucket=bucket, Key=key)
            else:  # bounded even if the object grew after the HEAD
                response = self._s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes}")
        except ClientError as err:
            if _error_code(err) in _MISSING:
                return None
            if _error_code(err) == "InvalidRange":  # emptied after the HEAD
                return b""
            raise
        body = response["Body"]
        try:
            data = body.read(max_bytes + 1)
        finally:
            body.close()
        total = response.get("ContentRange", "").rpartition("/")[2]
        if len(data) > max_bytes or (total.isdigit() and int(total) > max_bytes):
            raise ValueError(f"{key} is larger than {max_bytes} bytes")
        return data

    async def get_bytes(
        self, run_id: str, key: str, *, max_bytes: int = MAX_READ_BYTES
    ) -> bytes | None:
        bucket = bucket_name(run_id)
        return await asyncio.to_thread(self._get, bucket, check_run_key(key), max_bytes)

    async def put_bytes(self, run_id: str, key: str, data: bytes) -> None:
        bucket = bucket_name(run_id)
        await asyncio.to_thread(
            self._s3.put_object, Bucket=bucket, Key=check_run_key(key), Body=data
        )

    async def delete(self, run_id: str, key: str) -> None:
        bucket = bucket_name(run_id)
        await asyncio.to_thread(self._s3.delete_object, Bucket=bucket, Key=check_run_key(key))


def _delete_batch(client: S3Client, bucket: str, keys: list[str]) -> None:
    try:
        response = client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True}
        )
        failed = [e["Key"] for e in response.get("Errors", []) if "Key" in e]
    except ClientError as err:
        if _error_code(err) not in _UNENCODABLE:  # throttling etc.: never 1000x the requests
            raise
        failed = keys  # a planted key the XML body can't carry
    for key in failed:  # one by one: the key travels in the URL, not in an XML body
        client.delete_object(Bucket=bucket, Key=key)


def _empty(client: S3Client, bucket: str, max_objects: int) -> bool:
    removed = 0
    try:
        # Listing asks for url-encoded keys (botocore default) and decodes them.
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys = [obj["Key"] for obj in page.get("Contents", [])]
            for start in range(0, len(keys), DELETE_BATCH):
                if removed >= max_objects:
                    return False  # bounded work per call: the caller retries later
                batch = keys[start : start + DELETE_BATCH]
                _delete_batch(client, bucket, batch)
                removed += len(batch)
        # botocore encodes and decodes keys for ListObjects* only: ask for it here too.
        uploads = client.get_paginator("list_multipart_uploads").paginate(
            Bucket=bucket, EncodingType="url"
        )
        for uploads_page in uploads:
            encoded = uploads_page.get("EncodingType") == "url"  # a store may ignore it [S-11]
            for upload in uploads_page.get("Uploads", []):
                key = unquote(upload["Key"]) if encoded else upload["Key"]
                client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload["UploadId"])
    except ClientError as err:
        if _error_code(err) in _MISSING:
            return True
        raise
    return True


async def empty_bucket(client: S3Client, bucket: str, *, max_objects: int = MAX_EMPTY) -> bool:
    """Delete objects and pending multipart uploads (a bucket must be empty before it can be
    deleted [S-11]); run buckets have no versioning. At most about `max_objects` per call, so
    a bucket full of planted files can't block the caller for hours: False until it is empty."""
    return await asyncio.to_thread(_empty, client, bucket, max_objects)


class S3ArtifactStore:
    """ArtifactStore (app/storage/artifacts.py) on an S3 bucket: the control-plane artifacts and
    bundles in prod, where the endpoint's container disk is ephemeral (architecture ADR-002).
    Synchronous like the protocol: callers run it in a worker thread. Only the control plane
    writes this bucket, so reads are not size-bounded."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        if not bucket:
            raise ValueError("an artifact bucket is required")
        self._s3 = client
        self._bucket = bucket

    @classmethod
    def connect(
        cls,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        key_id: str,
        secret: SecretStr,
        allow_http: bool = False,
    ) -> "S3ArtifactStore":
        client = make_s3_client(endpoint_url, region, key_id, secret, allow_http=allow_http)
        return cls(client, bucket)

    def __repr__(self) -> str:
        return f"S3ArtifactStore(bucket={self._bucket!r})"

    def put_bytes(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=check_key(key), Body=data)

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=check_key(key))
        except ClientError as err:
            if _error_code(err) in _MISSING_KEY:  # a missing bucket is a misconfiguration
                raise FileNotFoundError(key) from None
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def put_json(self, key: str, data: Any) -> None:
        self.put_bytes(key, json.dumps(data, sort_keys=True, ensure_ascii=False).encode())

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_bytes(key))
