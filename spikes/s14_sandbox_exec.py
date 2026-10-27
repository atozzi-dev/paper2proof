"""Spike S14: can a Token Factory Sandbox run an exec job the way a Nebius job does?

Run:   uv run python spikes/s14_sandbox_exec.py --image <runner-cpu UUID> [--big-mib 64]
       (the UUID scripts/sandbox_import.py printed; without --image a public python image runs
       phases 1-4 only, and the verdict is at best PARTIAL: the gate needs phase 5)
Needs: NEBIUS_API_KEY and TF_PROJECT_ID (sent as the `Project` header), Sandboxes beta access,
       `zstd` on this host (phase 5 packs the canary archives). Run it from the repository root.
Cost:  five short disposable operations (seconds to a few minutes of CPU) and a few uploads.

Checks what SandboxesJobBackend relies on (docs/specs/hybrid-deploy.md, "Experiments in
Sandboxes"):
1. POST /v1/files uploads raw bytes with a Content-Length (a small file and a --big-mib random
   one) and returns the uuid and sha256; GET /v1/files/<sha256> finds it again (deduplication).
2. A spawn with networking disabled, as root, mounts both files through `files` at /mnt/run/...
   and /etc/p2p/... as root 0444; the command checks the digests, `setpriv` to uid 10001, that
   the inputs are read-only for it, CPU, memory and disk, and prints ~8 MiB of base64 after a
   marker with truncate_output_at = 10 MiB. It exits 3: the operation must report exit_code 3,
   and `metadata.env` must echo P2P_JOB_NAME (how a lost submission is found again).
3. A `sleep 600` instance cancelled with DELETE /v1/operations/<id> must end CANCELLED in 60 s.
4. GET /v1/operations with the backend's own query (unix `since`, limit 1000, offset 0) lists
   the operations; the facts say whether the items already carry `metadata.env`.
5. With --image: the backend's own job_command runs the canary workspace on the imported image,
   its inputs uploaded and mounted as the backend does. A normal run exits 0 and its harvest
   passes the control plane's results check; a run stopped at its watchdog exits 20 (the
   runner's budget_timeout) with partial results. The first run's duration is the cold start.
PASS = all of the above. Uploaded files are content-addressed and harmless.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spikelib import Report, http, require_env

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "https://api.tokenfactory.nebius.com/sandboxes"
NON_TERMINAL = {"PENDING", "ASSIGNED", "EXECUTING"}
MARKER = "P2P-S14-OUTPUT"
OUT_BYTES = 6 * 1024 * 1024  # ~8 MiB once base64-encoded
TRUNCATE_AT = 10 * 1024 * 1024
JOB_NAME = "p2p-exec-s14spike000000000000000000-1"  # a 26-character slug, as the backend's
BIG = "/mnt/run/staging/big.bin"
SMALL = "/etc/p2p/manifest.json"
ROOT_RO = {"uid": 0, "gid": 0, "mode": "0444"}  # how the backend mounts every input
PROBE = f"""
set -u
echo "STARTED: $(date +%s)"
echo "ID: $(id -u):$(id -g)"
echo "NPROC: $(nproc)"
echo "MEM_KB: $(awk '/MemTotal/ {{print $2}}' /proc/meminfo)"
echo "DF: $(df -Pk /work /tmp /root 2>/dev/null | tail -n +2 \
  | awk '{{print $6 "=" $4}}' | tr '\\n' ' ')"
echo "STAT_BIG: $(stat -c '%u:%g:%a' {BIG})"
echo "SHA_BIG: $(sha256sum {BIG} | cut -d' ' -f1)"
echo "SHA_SMALL: $(sha256sum {SMALL} | cut -d' ' -f1)"
echo "SETPRIV: $(command -v setpriv || echo missing)"
echo "ROOT_RW: $(install -d -m 0700 /root/p2p 2>/dev/null && echo yes || echo no)"
install -d -o 10001 -g 10001 /mnt/run/results/1
setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs sh -c '
  echo "CHILD_ID: $(id -u)"
  echo ok > /mnt/run/results/1/out.txt && echo "CHILD_WRITE: ok"
  (echo x >> {BIG}) 2>/dev/null && echo "CHILD_RO: writable" || echo "CHILD_RO: read-only"
  chmod 0666 {BIG} 2>/dev/null && echo "CHILD_CHMOD: yes" || echo "CHILD_CHMOD: no"
'
echo "RESULT_FILE: $(cat /mnt/run/results/1/out.txt 2>/dev/null)"
echo {MARKER}
head -c {OUT_BYTES} /dev/urandom | base64 -w0
echo
exit 3
"""
CANARY = """
import argparse, json, os
import canary_overlay  # from the overlay archive: proves it was extracted onto PYTHONPATH
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, required=True)
p.add_argument("--out", required=True)
p.add_argument("--steps", type=int, default=1)
a = p.parse_args()
with open(os.path.join(os.environ["P2P_DATA_DIR"], "canary", "offset.txt")) as f:
    offset = float(f.read())  # from the data archive
os.makedirs(a.out, exist_ok=True)
value = canary_overlay.BASE + offset + a.seed / 100 + a.steps / 1000
with open(os.path.join(a.out, "metrics.json"), "w") as f:
    json.dump({"top1_accuracy": value, "baseline/top1_accuracy": value - 0.1}, f)
print(f"seed {a.seed}: {value}")
"""
SLOW = CANARY.replace('print(f"seed', 'import time; time.sleep(25); print(f"seed')


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
        self.executing_at: float | None = None  # wall time the operation was first EXECUTING
        while time.monotonic() - start < limit_s:
            op = self.call("GET", f"/v1/operations/{op_id}").json() or {}
            if op.get("status") == "EXECUTING" and self.executing_at is None:
                self.executing_at = time.time()
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
    resp = sbx.call("POST", "/v1/files", data=data)  # urllib sends a Content-Length
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


def _tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _zstd(zstd: str, data: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.tar"
        src.write_bytes(data)
        subprocess.run([zstd, "-q", "-f", str(src), "-o", f"{src}.zst"], check=True)  # noqa: S603
        return Path(f"{src}.zst").read_bytes()


def _manifest(workspace_sha: str, overlay_sha: str, data_sha: str, budget_s: int) -> Any:
    from app.domain.contracts import ExecManifest

    return ExecManifest.model_validate(
        {
            "schema": "p2p.manifest/v2", "run_id": "r_01jb7z00000000000000000000", "attempt": 1,
            "image_digest": "sha256:" + "ab" * 32, "entrypoint": "workspace/train.py",
            "args": ["--steps", "3"], "seeds": [0, 1, 2], "budget_seconds": budget_s,
            "paths": {
                "overlay": "/mnt/run/staging/overlay.tar.zst", "repo": None,
                "data_dir": "/mnt/run/staging/data",
                "workspace": "/mnt/run/staging/workspace.tar", "cache": "/mnt/cache",
                "results": "/mnt/run/results/1",
            },
            "claims": [{"claim_ref": "c1", "claim_type": "RELATIVE", "metric": "top1_accuracy",
                        "higher_is_better": True, "metric_key": "top1_accuracy",
                        "baseline_name": "resnet18", "baseline_key": "baseline/top1_accuracy"}],
            "scale": {"full": False},
            "archives": {"overlay": overlay_sha, "repo": None,
                         "data": [{"name": "canary", "sha256": data_sha}]},
            "workspace_sha256": workspace_sha,
        }
    )  # fmt: skip


def job_phase(sbx: Sandboxes, r: Report, image: str) -> None:
    """The backend's job command on the imported runner-cpu image (phase 5)."""
    sys.path.insert(0, str(ROOT))
    from app.execution.backend import JobKind
    from app.execution.sandbox_jobs import INSTANCE_SLACK_S, job_command, parse_harvest
    from app.execution.smoke import pack_with_digest
    from app.verdict.results import analyze_results

    zstd = shutil.which("zstd")
    if zstd is None:
        r.partial("no zstd on this host: phase 5 skipped (apt install zstd)")
        return
    overlay = _zstd(zstd, _tar({"canary_overlay.py": "BASE = 0.5\n"}))
    data = _zstd(zstd, _tar({"offset.txt": "0.25\n"}))
    cases = [  # label, script, budget, watchdog, exit code, exit reason, seeds
        ("normal", CANARY, 120, 300, 0, "completed", [0, 1, 2]),
        ("watchdog", SLOW, 120, 100, 20, "budget_timeout", [0]),  # TERM at 40 s, 25 s a seed
    ]
    for label, script, budget, watchdog, want_code, want_reason, want_seeds in cases:
        workspace, workspace_sha = pack_with_digest({"train.py": script.encode()})
        manifest = _manifest(
            workspace_sha, hashlib.sha256(overlay).hexdigest(), hashlib.sha256(data).hexdigest(),
            budget,
        )  # fmt: skip
        inputs = {
            "/mnt/run/staging/overlay.tar.zst": overlay,
            "/mnt/run/staging/data/canary.tar.zst": data,
            "/mnt/run/staging/workspace.tar": workspace,
            "/etc/p2p/manifest.json": manifest.model_dump_json(by_alias=True).encode(),
        }
        files = {}
        for path, content in inputs.items():
            uuid = upload(sbx, r, f"{label} {Path(path).name}", content)
            if uuid is None:
                return
            files[path] = {"uuid": uuid, **ROOT_RO}
        marker = secrets.token_hex(16)
        op_id, err = sbx.spawn(
            {
                "image": image,
                "command": job_command(
                    kind=JobKind.EXEC, out_dir="/mnt/run/results/1", watchdog_s=watchdog,
                    marker=marker,
                ),
                "shell": True, "uid": 0, "gid": 0, "cwd": "/work",
                "env": {"PYTHONUNBUFFERED": "1", "P2P_JOB_NAME": JOB_NAME},
                "networking": {"enabled": False}, "disposable": True,
                "timeout": watchdog + INSTANCE_SLACK_S, "truncate_output_at": TRUNCATE_AT,
                "files": files,
            }
        )  # fmt: skip
        if op_id is None:
            r.fail(f"job {label}: {err}")
            continue
        op, took = sbx.wait(op_id, watchdog + INSTANCE_SLACK_S + 300)
        result = (op.get("metadata") or {}).get("result") or {}
        code = (result.get("state") or {}).get("exit_code")
        harvest = parse_harvest(decode(result.get("stdout")), marker, "exec")
        r.fact(f"job {label}: status {op.get('status')} in {took} s, exit code {code}")
        r.fact(f"job {label}: harvested {sorted(harvest or {})}")
        if code != want_code:
            r.fail(f"job {label}: exit code {code}, expected {want_code}")
            r.fact(f"job {label}: stderr tail {decode(result.get('stderr'))[-600:]!r}")
        if not harvest or "results.json" not in harvest:
            r.fail(f"job {label}: no results.json in the harvest")
            continue
        analysis = analyze_results(harvest["results.json"], manifest)
        r.fact(f"job {label}: {analysis.exit_reason}, seeds {analysis.used_seeds}")
        if analysis.problem is not None:
            r.fail(f"job {label}: results.json refused: {analysis.problem}")
        elif (analysis.exit_reason, analysis.used_seeds) != (want_reason, want_seeds):
            r.fail(f"job {label}: expected {want_reason} with seeds {want_seeds}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--image", help="the imported runner-cpu UUID (scripts/sandbox_import.py)")
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
    started = time.monotonic()
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
            "files": {BIG: {"uuid": big_id, **ROOT_RO}, SMALL: {"uuid": small_id, **ROOT_RO}},
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
        r.fact(
            f"exec: status {op.get('status')} in {took} s (cold start), error {op.get('error')!r}"
        )
        r.fact(f"exec: state {result.get('state')}")
        r.fact(f"exec: resources {result.get('resources')}")
        if sbx.executing_at is not None and line(head, "STARTED").isdigit():
            gap = int(line(head, "STARTED")) - sbx.executing_at  # polled every second: +-1 s
            r.fact(f"exec: the command started {gap:.0f} s after EXECUTING was first seen")
            if gap > 25:
                r.partial("the command starts > 25 s after EXECUTING: raise HOST_MARGIN_S")
        for key in ("ID", "NPROC", "MEM_KB", "DF", "STAT_BIG", "SETPRIV", "ROOT_RW", "CHILD_ID"):
            r.fact(f"exec: {key} = {line(head, key)}")
        r.fact(
            f"exec: CHILD_WRITE = {line(head, 'CHILD_WRITE')}, RO = {line(head, 'CHILD_RO')}, "
            f"CHMOD = {line(head, 'CHILD_CHMOD')}"
        )
        r.fact(f"exec: stdout {len(stdout)} chars, truncated {stdout_stream.get('truncated')}")
        r.fact(f"exec: stderr {decode(result.get('stderr'))[:300]!r}")
        if (result.get("state") or {}).get("exit_code") != 3:
            r.fail("exit code 3 was not reported")
        if line(head, "SHA_BIG") != hashlib.sha256(big).hexdigest():
            r.fail("the big file mounted with another digest")
        if line(head, "SHA_SMALL") != hashlib.sha256(small).hexdigest():
            r.fail("the small file mounted with another digest")
        if line(head, "STAT_BIG") != "0:0:444":
            r.partial("mounted file owner or mode differ from the request")
        if line(head, "SETPRIV") == "missing" or line(head, "CHILD_ID") != "10001":
            r.fail("could not drop to uid 10001 with setpriv")
        if line(head, "CHILD_RO") != "read-only" or line(head, "CHILD_CHMOD") != "no":
            r.fail("uid 10001 could change a mounted input file")
        if line(head, "ROOT_RW") != "yes":
            r.partial("/root is not writable: the job command falls back to mktemp -d")
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

    print("== list operations (the backend's query)")
    since = int(time.time() - (time.monotonic() - started) - 600)
    listed = sbx.call("GET", f"/v1/operations?kind=instance&since={since}&limit=1000&offset=0")
    items = listed.json()
    items = items if isinstance(items, list) else (items or {}).get("operations", [])
    ids = {str(i.get("uuid")) for i in items if isinstance(i, dict)}
    r.fact(
        f"list: HTTP {listed.status}, {len(ids)} operations, shape {type(listed.json()).__name__}"
    )
    if items and isinstance(items[0], dict):
        r.fact(f"list: item keys {sorted(items[0])}")
        r.fact(f"list: items carry metadata.env: {'env' in (items[0].get('metadata') or {})}")
    if listed.status != 200 or (op_id and op_id not in ids):
        r.fail("the backend's operation query does not list the exec operation")

    if args.image:
        print("== the backend's job command on the imported image")
        job_phase(sbx, r, image)
    else:
        r.partial("no --image: the job command was not run on the imported runner-cpu (phase 5)")

    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
