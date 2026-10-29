"""Import p2p-runner-cpu into Token Factory Sandboxes and record its image UUID
(docs/specs/hybrid-deploy.md, "Experiments in Sandboxes"; docs/DEPLOY.md §Hybrid).

Run:   uv run python scripts/sandbox_import.py --image <registry>/p2p-runner-cpu@sha256:<digest>
       [--pull-tag <tag>]   (the importer refuses an OCI index referenced by digest: pass the
       tag that `docker buildx imagetools inspect <registry>/p2p-runner-cpu:<tag>` shows with
       that digest; the UUID is still recorded under the digest)
Needs: NEBIUS_API_KEY and TF_PROJECT_ID. For a private registry: --username (the Nebius
       registry takes `iam`) and the password in P2P_REGISTRY_PASSWORD (an IAM access token);
       Sandboxes uses it for this import only. Nothing secret is printed or written.

1. POST /v1/images/import with the image by digest (invariant 8), then wait for the operation.
2. Check the imported image offline: the runner files, setpriv, torch and the OpenMP
   runtime (libgomp, which LightGBM and XGBoost wheels load) are there.
3. Write `<digest>: <image uuid>` into config/sandbox_images.yaml (other digests are kept).
Exit 0 on success; the file is then committed like any config change.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml

API = "https://api.tokenfactory.nebius.com/sandboxes"
IMAGE_REF = re.compile(r"^[a-z0-9.\-/:]+@(sha256:[0-9a-f]{64})$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
NON_TERMINAL = {"PENDING", "ASSIGNED", "EXECUTING"}
# what the job command needs, run as the job runs it (root, then uid 10001 through setpriv)
CHECK = (
    "test -r /opt/p2p/entrypoint.py && test -r /opt/p2p/harvest.py"
    " && command -v setpriv setsid timeout tail >/dev/null"
    " && install -d -m 0700 /root/p2p"
    ' && echo "uid $(setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs id -u)"'
    ' && /opt/venv/bin/python -I -c \'import ctypes, torch; ctypes.CDLL("libgomp.so.1");'
    ' print("torch", torch.__version__)\''
)
IMPORT_TIMEOUT_S = 1800


class ImportFailed(RuntimeError):
    pass


class Sandboxes:
    def __init__(self, key: str, project: str, client: httpx.Client) -> None:
        self._headers = {"Authorization": f"Bearer {key}", "Project": project}
        self._client = client

    def call(self, method: str, path: str, body: Any = None) -> Any:
        response = self._client.request(method, f"{API}{path}", json=body, headers=self._headers)
        if response.status_code not in (200, 201, 202):
            raise ImportFailed(f"{method} {path}: HTTP {response.status_code}")
        return response.json()

    def wait(self, op_id: str, limit_s: float, sleep: Callable[[float], None]) -> dict[str, Any]:
        deadline = time.monotonic() + limit_s
        while True:
            op = self.call("GET", f"/v1/operations/{op_id}")
            if op.get("status") not in NON_TERMINAL:
                return dict(op)
            if time.monotonic() > deadline:
                raise ImportFailed(f"operation {op_id} did not finish in {limit_s:.0f} s")
            sleep(5)


def _stdout(op: dict[str, Any]) -> str:
    stream = ((op.get("metadata") or {}).get("result") or {}).get("stdout") or {}
    value = str(stream.get("value") or "")
    if stream.get("encoding") == "base64":
        try:
            return base64.b64decode(value).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""
    return value


def import_image(
    sbx: Sandboxes,
    image: str,
    credentials: dict[str, str] | None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """The UUID of `image` imported into Sandboxes, after an offline check of its contents."""
    registry: dict[str, Any] = {"url": f"docker://{image}"}
    if credentials:
        registry["credentials"] = credentials
    started = sbx.call(
        "POST", "/v1/images/import", {"registry": registry, "timeout": IMPORT_TIMEOUT_S}
    )
    op = sbx.wait(str(started["uuid"]), IMPORT_TIMEOUT_S + 120, sleep)
    uuid = (op.get("result") or {}).get("image") or op.get("result_image_uuid")
    if op.get("status") != "SUCCESS" or not uuid:
        raise ImportFailed(f"import ended {op.get('status')}: {str(op.get('error'))[:300]}")
    check = sbx.call(
        "POST",
        "/v1/instances",
        {
            "image": uuid,
            "command": CHECK,
            "shell": True,
            "uid": 0,
            "gid": 0,
            "networking": {"enabled": False},
            "disposable": True,
            "timeout": 120,
        },
    )
    ran = sbx.wait(str(check["uuid"]), 300, sleep)
    code = (((ran.get("metadata") or {}).get("result") or {}).get("state") or {}).get("exit_code")
    out = _stdout(ran)
    if code != 0 or "uid 10001" not in out or "torch" not in out:
        raise ImportFailed(
            "the imported image cannot run the job command (runner files, setpriv, setsid, "
            "timeout, a writable /root, the uid 10001 drop, torch or the OpenMP runtime)"
        )
    print(f"checked: {_stdout(ran).strip().splitlines()[-1]}")
    return str(uuid)


def record(config: Path, digest: str, uuid: str) -> None:
    data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.is_file() else None
    images = dict((data or {}).get("images") or {})
    images[digest] = uuid
    header = (
        "# Token Factory Sandboxes image UUIDs, one per runner-cpu digest (sandbox_import.py)\n"
    )
    config.write_bytes((header + yaml.safe_dump({"images": images}, sort_keys=True)).encode())


def main(argv: Sequence[str] | None = None, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="<registry>/p2p-runner-cpu@sha256:...")
    parser.add_argument("--username", help="registry user (the Nebius registry takes `iam`)")
    parser.add_argument(
        "--pull-tag",
        help="import <registry>/<name>:<tag> instead (the importer refuses an OCI index by "
        "digest); check first that the tag resolves to --image's digest",
    )
    parser.add_argument("--config", type=Path, default=Path("config/sandbox_images.yaml"))
    args = parser.parse_args(argv)
    match = IMAGE_REF.fullmatch(args.image)
    if match is None:
        print("--image must be referenced by digest: <registry>/<name>@sha256:<64 hex>")
        return 2
    pull = args.image
    if args.pull_tag is not None:
        if not TAG.fullmatch(args.pull_tag):
            print("--pull-tag is not a valid tag")
            return 2
        pull = f"{args.image.split('@', 1)[0]}:{args.pull_tag}"
        print(
            f"importing {pull}: it must resolve to {match.group(1)} "
            "(check with docker buildx imagetools inspect)"
        )
    key, project = os.environ.get("NEBIUS_API_KEY"), os.environ.get("TF_PROJECT_ID")
    if not key or not project:
        print("NEBIUS_API_KEY and TF_PROJECT_ID are required")
        return 2
    credentials = None
    if args.username:
        password = os.environ.get("P2P_REGISTRY_PASSWORD")
        if not password:
            print("--username needs the password in P2P_REGISTRY_PASSWORD")
            return 2
        credentials = {"username": args.username, "password": password}
    with client or httpx.Client(timeout=60) as http:
        try:
            uuid = import_image(Sandboxes(key, project, http), pull, credentials)
        except (ImportFailed, httpx.HTTPError) as err:
            print(f"FAILED: {err}")
            return 1
    record(args.config, match.group(1), uuid)
    print(f"{match.group(1)}: {uuid} written to {args.config}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
