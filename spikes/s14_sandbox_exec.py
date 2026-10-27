"""Spike S14: can a Token Factory Sandbox run an exec job the way a Nebius job does?

Run:   uv run python spikes/s14_sandbox_exec.py [--image UUID|tag:NAME[:TAG]] [--big-mib 64]
Needs: NEBIUS_API_KEY and TF_PROJECT_ID (sent as the `Project` header). Sandboxes beta access.
Cost:  three short disposable operations (seconds to a minute of CPU) and two uploads.

Checks what the Sandboxes exec backend relies on (docs/specs/hybrid-deploy.md, plan B):
1. POST /v1/files uploads raw bytes (a small file and a --big-mib random one) and returns the
   uuid and sha256; GET /v1/files/<sha256> finds the file again (upload deduplication).
2. A spawn with networking disabled, run as root, mounts both files through `files` at
   /mnt/run/staging/... and /etc/p2p/... with uid 10001 and mode 0444; the command checks the
   digests, `setpriv` to uid 10001, that the mounted files are read-only for it, CPU, memory
   and disk, and prints ~8 MiB of base64 after a marker with truncate_output_at = 10 MiB.
   It exits 3: the operation must report exit_code 3, and `metadata.env` must echo
   P2P_JOB_NAME (how a lost submission is found again).
3. A `sleep 600` instance cancelled with DELETE /v1/operations/<id> after a few seconds must
   end CANCELLED within 60 s.
4. GET /v1/operations?kind=instance&since=1h lists the three operations.
PASS = all of the above. Uploaded files are content-addressed and harmless (random bytes).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spikelib import Report, http, require_env

DEFAULT_BASE = "https://api.tokenfactory.nebius.com/sandboxes"
NON_TERMINAL = {"PENDING", "ASSIGNED", "EXECUTING"}
MARKER = "P2P-S14-OUTPUT"
OUT_BYTES = 6 * 1024 * 1024  # ~8 MiB once base64-encoded
TRUNCATE_AT = 10 * 1024 * 1024
JOB_NAME = "p2p-exec-s14spike0000000000000000000-1"
BIG = "/mnt/run/staging/big.bin"
SMALL = "/etc/p2p/manifest.json"
PROBE = f"""
set -u
echo "ID: $(id -u):$(id -g)"
echo "NPROC: $(nproc)"
echo "MEM_KB: $(awk '/MemTotal/ {{print $2}}' /proc/meminfo)"
echo "DF: $(df -Pk /work /tmp 2>/dev/null | tail -n +2 | awk '{{print $6 "=" $4}}' | tr '\\n' ' ')"
echo "STAT_BIG: $(stat -c '%u:%g:%a' {BIG})"
echo "SHA_BIG: $(sha256sum {BIG} | cut -d' ' -f1)"
echo "SHA_SMALL: $(sha256sum {SMALL} | cut -d' ' -f1)"
echo "SETPRIV: $(command -v setpriv || echo missing)"
install -d -o 10001 -g 10001 /mnt/run/results/1
setpriv --reuid=10001 --regid=10001 --clear-groups sh -c '
  echo "CHILD_ID: $(id -u)"
  echo ok > /mnt/run/results/1/out.txt && echo "CHILD_WRITE: ok"
  (echo x >> {BIG}) 2>/dev/null && echo "CHILD_RO: writable" || echo "CHILD_RO: read-only"
'
echo "RESULT_FILE: $(cat /mnt/run/results/1/out.txt 2>/dev/null)"
echo {MARKER}
head -c {OUT_BYTES} /dev/urandom | base64 -w0
echo
exit 3
"""


class Sandboxes:
    def __init__(self, base: str, key: str, project: str) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {key}", "Project": project}

    def call(self, method: str, path: str, body: Any = None, data: bytes | None = None) -> Any:
        return http(
            method, self.base + path, headers=self.headers, body=body, data=data, timeout=300
        )

    def spawn(self, spec: dict[str, Any]) -> tuple[str | None, str]:
        resp = self.call("POST", "/v1/instances", spec)
        if resp.status not in (200, 201):
            return None, f"spawn HTTP {resp.status} {resp.text(300)!r}"
        return (resp.json() or {}).get("uuid"), ""

    def wait(self, op_id: str, limit_s: float) -> tuple[dict[str, Any], float]:
        start = time.monotonic()
        op: Any = {}
        while time.monotonic() - start < limit_s:
            op = self.call("GET", f"/v1/operations/{op_id}").json() or {}
            if op.get("status") not in NON_TERMINAL:
                break
            time.sleep(1)
        return op, round(time.monotonic() - start, 1)


def decode(stream: Any) -> str:
    if not isinstance(stream, dict):
        return ""
    value = str(stream.get("value") or "")
    if stream.get("encoding") == "base64":
        value = base64.b64decode(value).decode("utf-8", errors="replace")
    return value


def pick_image(sbx: Sandboxes) -> str | None:
    images = (sbx.call("GET", "/v1/images?tag=python&limit=20").json() or {}).get("images", [])
    return str(images[0]["uuid"]) if images else None


def line(stdout: str, key: str) -> str:
    for row in stdout.splitlines():
        if row.startswith(f"{key}: "):
            return row.removeprefix(f"{key}: ").strip()
    return ""


def upload(sbx: Sandboxes, r: Report, label: str, data: bytes) -> str | None:
    digest = hashlib.sha256(data).hexdigest()
    start = time.monotonic()
    resp = sbx.call("POST", "/v1/files", data=data)
    took = round(time.monotonic() - start, 1)
    if resp.status not in (200, 201):
        r.fail(f"upload {label} HTTP {resp.status} {resp.text(300)!r}")
        return None
    body = resp.json() or {}
    r.fact(f"upload {label}: {len(data)} bytes in {took} s, response keys {sorted(body)}")
    if body.get("sha256") != digest:
        r.fail(f"upload {label}: sha256 {body.get('sha256')!r} != {digest}")
    found = sbx.call("GET", f"/v1/files/{digest}")
    if found.status != 200 or (found.json() or {}).get("uuid") != body.get("uuid"):
        r.partial(f"GET /v1/files/<sha256> for {label}: HTTP {found.status} {found.text(200)!r}")
    else:
        r.fact(f"dedup lookup {label}: OK")
    uuid = body.get("uuid")
    return str(uuid) if uuid else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--image", help="image UUID or tag:<name>[:<tag>] (needs coreutils)")
    parser.add_argument("--big-mib", type=int, default=64)
    args = parser.parse_args()

    env = require_env("NEBIUS_API_KEY", "TF_PROJECT_ID")
    sbx = Sandboxes(args.base_url, env["NEBIUS_API_KEY"], env["TF_PROJECT_ID"])
    r = Report()

    print("== whoami")
    who = sbx.call("GET", "/v1/whoami")
    if who.status != 200:
        r.fail(f"whoami HTTP {who.status} {who.text(300)!r}")
        r.summary()
        print("VERDICT: FAIL")
        return 1
    r.fact(f"limits: {(who.json() or {}).get('limits')}")
    image = args.image or pick_image(sbx)
    if not image:
        r.fail("no public python image found; pass --image")
        r.summary()
        print("VERDICT: FAIL")
        return 1
    r.fact(f"image: {image}")

    print("== uploads")
    small = json.dumps({"spike": "s14", "nonce": os.urandom(8).hex()}).encode()
    big = os.urandom(args.big_mib * 1024 * 1024)
    small_id, big_id = upload(sbx, r, "small", small), upload(sbx, r, "big", big)
    if not (small_id and big_id):
        r.summary()
        print("VERDICT: FAIL")
        return 1

    print("== exec-like instance (networking disabled, files mounted, setpriv, big stdout)")
    mode = {"uid": 10001, "gid": 10001, "mode": "0444"}
    op_id, err = sbx.spawn(
        {
            "image": image,
            "command": PROBE,
            "shell": True,
            "uid": 0,
            "gid": 0,
            "cwd": "/work",
            "env": {"P2P_JOB_NAME": JOB_NAME, "PYTHONUNBUFFERED": "1"},
            "networking": {"enabled": False},
            "disposable": True,
            "timeout": 300,
            "truncate_output_at": TRUNCATE_AT,
            "files": {BIG: {"uuid": big_id, **mode}, SMALL: {"uuid": small_id, **mode}},
        }
    )
    if op_id is None:
        r.fail(err)
    else:
        op, took = sbx.wait(op_id, 420)
        meta = op.get("metadata") or {}
        result = meta.get("result") or {}
        stdout_stream = result.get("stdout") or {}
        stdout = decode(stdout_stream)
        head, _, tail = stdout.partition(f"\n{MARKER}\n")
        r.fact(f"exec: status {op.get('status')} in {took} s, error {op.get('error')!r}")
        r.fact(f"exec: state {result.get('state')}")
        r.fact(f"exec: resources {result.get('resources')}")
        for key in ("ID", "NPROC", "MEM_KB", "DF", "STAT_BIG", "SETPRIV", "CHILD_ID"):
            r.fact(f"exec: {key} = {line(head, key)}")
        r.fact(f"exec: CHILD_WRITE = {line(head, 'CHILD_WRITE')}, RO = {line(head, 'CHILD_RO')}")
        r.fact(f"exec: stdout {len(stdout)} chars, truncated {stdout_stream.get('truncated')}")
        r.fact(f"exec: stderr {decode(result.get('stderr'))[:300]!r}")
        if (result.get("state") or {}).get("exit_code") != 3:
            r.fail("exit code 3 was not reported")
        if line(head, "SHA_BIG") != hashlib.sha256(big).hexdigest():
            r.fail("the big file mounted with another digest")
        if line(head, "SHA_SMALL") != hashlib.sha256(small).hexdigest():
            r.fail("the small file mounted with another digest")
        if line(head, "STAT_BIG") != "10001:10001:444":
            r.partial("mounted file owner or mode differ from the request")
        if line(head, "SETPRIV") == "missing" or line(head, "CHILD_ID") != "10001":
            r.fail("could not drop to uid 10001 with setpriv")
        if line(head, "CHILD_RO") != "read-only":
            r.partial("uid 10001 could append to a mounted input file")
        if line(head, "RESULT_FILE") != "ok":
            r.fail("uid 10001 could not write the results directory")
        try:
            decoded = len(base64.b64decode(tail.strip(), validate=True))
        except ValueError:
            decoded = -1
        if decoded != OUT_BYTES or stdout_stream.get("truncated"):
            r.fail(f"big stdout came back as {decoded} bytes (expected {OUT_BYTES})")
        echoed = (meta.get("env") or {}).get("P2P_JOB_NAME")
        if echoed != JOB_NAME:
            r.partial(f"metadata.env does not echo P2P_JOB_NAME ({echoed!r})")
        else:
            r.fact("exec: metadata.env echoes P2P_JOB_NAME")

    print("== cancel a running instance")
    sleep_id, err = sbx.spawn(
        {
            "image": image,
            "command": "sleep 600",
            "shell": True,
            "networking": {"enabled": False},
            "disposable": True,
            "timeout": 900,
        }
    )
    if sleep_id is None:
        r.fail(err)
    else:
        time.sleep(8)
        resp = sbx.call("DELETE", f"/v1/operations/{sleep_id}")
        r.fact(f"cancel: HTTP {resp.status}")
        op, took = sbx.wait(sleep_id, 60)
        r.fact(f"cancel: status {op.get('status')} {took} s after the DELETE")
        if op.get("status") != "CANCELLED":
            r.fail("a cancelled instance did not end CANCELLED within 60 s")
            sbx.call("DELETE", f"/v1/operations/{sleep_id}")

    print("== list operations")
    listed = sbx.call("GET", "/v1/operations?kind=instance&since=1h&limit=50")
    items = listed.json()
    items = items if isinstance(items, list) else (items or {}).get("operations", [])
    ids = {str(i.get("uuid")) for i in items if isinstance(i, dict)}
    r.fact(
        f"list: HTTP {listed.status}, {len(ids)} operations, shape {type(listed.json()).__name__}"
    )
    if items and isinstance(items[0], dict):
        r.fact(f"list: item keys {sorted(items[0])}")
    if op_id and op_id not in ids:
        r.partial("the exec operation is missing from the list")

    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
