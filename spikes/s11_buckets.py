"""Spike S11: per-run buckets: name rules, creation latency, quota, mount by name in a job.

Run:   python3 spikes/s11_buckets.py [--count 5] [--job-image IMAGE] [--subnet-id ID]   (WSL/Linux)
Needs: nebius CLI logged in, parent-id = p2p-sandbox project. Free (empty buckets, deleted after).

1. Name rules: tries a 34-char `p2p-run-<26 chars>` name, a 63-char name, a 64-char name and a
   name with an underscore (expected: first two accepted, last two rejected).
2. Creates and deletes --count buckets with production-shaped names, timing create, get-by-name
   and delete. Any quota error is printed verbatim.
3. With --job-image: one CPU job mounts a bucket BY NAME at /mnt/run and writes a file
   (uses spikes/net_probe.py), proving the mount syntax the orchestrator will use.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spikelib import Report

SPIKES = Path(__file__).resolve().parent
CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def slug() -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(26))


def nebius(*args: str, timeout: int = 300) -> tuple[int, str, float]:
    """Run the CLI; returns (exit code, stdout, seconds). stderr is shown only on failure."""
    exe = shutil.which("nebius")
    if exe is None:
        raise SystemExit("nebius CLI not found on PATH\nVERDICT: FAIL")
    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603 (fixed CLI, args built by this script)
        [exe, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    out = proc.stdout.strip() if proc.returncode == 0 else (proc.stdout + proc.stderr).strip()
    return proc.returncode, out, time.monotonic() - start


def create(name: str) -> tuple[bool, str, float]:
    code, out, secs = nebius("storage", "bucket", "create", "--name", name, "--format", "json")
    return code == 0, out, secs


def bucket_id(name: str) -> tuple[str, float]:
    code, out, secs = nebius(
        "storage", "bucket", "get-by-name", "--name", name, "--format", "jsonpath={.metadata.id}"
    )
    return (out.strip() if code == 0 else ""), secs


def delete(name: str) -> tuple[bool, float]:
    bid, _ = bucket_id(name)
    if not bid:
        return False, 0.0
    code, _, secs = nebius("storage", "bucket", "delete", "--id", bid)
    return code == 0, secs


def name_rules(r: Report) -> None:
    cases = {
        "34 chars (production shape)": f"p2p-run-{slug()}",
        "63 chars": ("p2p-run-" + slug() + slug() + "x" * 63)[:63],
        "64 chars": ("p2p-run-" + slug() + slug() + "x" * 64)[:64],
        "underscore": f"p2p_run_{slug()}",
    }
    for label, name in cases.items():
        ok, out, secs = create(name)
        r.fact(f"name rule {label} ({len(name)}): {'accepted' if ok else 'rejected'} {secs:.1f}s")
        if ok:
            delete(name)
        else:
            r.fact(f"  -> {out[:200]}")
            if label.startswith("34"):
                r.fail("production-shaped bucket name rejected")


def lifecycle(r: Report, count: int) -> list[str]:
    names = [f"p2p-run-{slug()}" for _ in range(count)]
    created: list[str] = []
    for name in names:
        ok, out, secs = create(name)
        if not ok:
            r.fail(f"create {name} failed after {len(created)} buckets: {out[:300]}")
            break
        created.append(name)
        bid, gsecs = bucket_id(name)
        r.fact(f"create {secs:.1f}s, get-by-name {gsecs:.1f}s ({'found' if bid else 'NOT found'})")
    return created


def mount_job(r: Report, bucket: str, image: str, subnet: str | None) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as spec:
        json.dump({"mount": "/mnt/run", "tcp": [], "tls": []}, spec)
    name = f"p2p-spike-s11-{int(time.time())}"
    args = [
        "ai", "job", "create", "--name", name, "--image", image,
        "--platform", "cpu-d3", "--preset", "2vcpu-8gb", "--disk-size", "30Gi",
        "--timeout", "1h", "--restart-policy", "never",
        "--container-command", "python", "--args", "/etc/p2p/net_probe.py",
        "--inject-file", f"{SPIKES / 'net_probe.py'}:/etc/p2p/net_probe.py",
        "--inject-file", f"{spec.name}:/etc/p2p/probe.json",
        "--volume", f"{bucket}:/mnt/run:rw", "--format", "json",
    ]  # fmt: skip
    if subnet:
        args += ["--subnet-id", subnet]
    job_id = ""
    try:
        code, out, _ = nebius(*args)  # inside try: a timeout or Ctrl-C here still cleans up
        if code != 0:
            r.fail(f"mount job create failed: {out[:300]}")
            return
        try:
            job_id = json.loads(out).get("metadata", {}).get("id", "")
        except json.JSONDecodeError:
            job_id = job_id_by_name(name)
        deadline = time.monotonic() + 900
        logs = ""
        while job_id and time.monotonic() < deadline:
            time.sleep(15)
            _, logs, _ = nebius("ai", "job", "logs", job_id)
            if "PROBE_END" in logs:
                break
        mount_lines = [line for line in logs.splitlines() if line.startswith("MOUNT")]
        r.fact(f"mount job {job_id or name}: {mount_lines or 'no probe output'}")
        if not any(line.startswith("MOUNT_OK") for line in mount_lines):
            r.fail("bucket mount by name did not work")
    finally:  # also on Ctrl-C: never leave the job running
        job_id = job_id or job_id_by_name(name)
        if job_id and nebius("ai", "job", "delete", job_id)[0] != 0:
            r.partial(f"job {job_id} not deleted: nebius ai job delete {job_id}")
        Path(spec.name).unlink(missing_ok=True)


def job_id_by_name(name: str) -> str:
    code, out, _ = nebius("ai", "job", "list", "--format", "json")
    if code != 0:
        return ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return ""
    items = data.get("items", []) if isinstance(data, dict) else data
    for item in items or []:
        if item.get("metadata", {}).get("name") == name:
            return str(item["metadata"]["id"])
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--job-image", help="image by digest for the mount test (optional)")
    parser.add_argument("--subnet-id")
    args = parser.parse_args()

    if args.job_image and sys.platform == "win32":
        # "C:\...:/etc/p2p/x" is ambiguous for --inject-file; run the mount test from WSL/Linux.
        print("--job-image is not supported on native Windows; run from WSL or Linux")
        print("VERDICT: FAIL")
        return 2
    r = Report()
    print("== name rules")
    name_rules(r)
    print(f"== create/get/delete x{args.count}")
    created = lifecycle(r, args.count)
    try:
        if args.job_image and created:
            print("== mount by name in a job")
            mount_job(r, created[0], args.job_image, args.subnet_id)
    finally:
        for name in created:
            ok, secs = delete(name)
            r.fact(f"delete {name}: {'ok' if ok else 'FAILED'} {secs:.1f}s")
            if not ok:
                r.partial(f"bucket {name} not deleted, remove it by hand")
    if not args.job_image:
        r.partial("mount-by-name not tested (pass --job-image)")

    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
