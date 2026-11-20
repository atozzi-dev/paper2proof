"""Spike S15: how much disk does a Token Factory Sandbox instance give a job?

Run:   uv run python spikes/s15_sandbox_disk.py [--image <runner-cpu UUID>] [--cap-gib 48]
       control runs: --cap-gib 0 --sleep 90 (idle), --cap-gib 0 --burn 5 (CPU, no writes)
       (default image: the last one in config/sandbox_images.yaml)
Needs: NEBIUS_API_KEY and TF_PROJECT_ID (the `Project` header), Sandboxes beta access.
Cost:  one disposable instance, networking disabled, for at most --timeout seconds (default
       600: about 0.013 USD at the sandbox price in config/pricing.yaml).

The 2026-10-30 eval lost InterpretML to "No space left on device" in a sandbox (A-51). This
spike measures what A-51's follow-up (C: check the inputs' size before starting) needs:
1. `df` of every mount, and of the paths a job writes (/tmp, /work, /mnt/run, /root).
2. How many bytes /tmp really takes before ENOSPC (dd in 64 MiB blocks, at most --cap-gib), and
   the exact error text, which A-51's classifier (`_DISK_FULL` in sandbox_jobs.py) must match
   when the platform reports it.
3. Whether the operation still ends with an exit code after its disk filled up.
PASS = all three measured. The fill file is deleted before the command exits.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s14_sandbox_exec import DEFAULT_BASE, Sandboxes, decode, line
from spikelib import Report, require_env

ROOT = Path(__file__).resolve().parents[1]
DISK_FULL = re.compile(r"No space left on device|\bos error 28\b|\bENOSPC\b")  # as sandbox_jobs


def probe(cap_gib: int, sleep_s: int, burn_s: int) -> str:
    blocks = cap_gib * 16  # 64 MiB blocks
    return f"""
set -u
echo "DF_ALL:"
df -PkT 2>/dev/null
echo "DF_PATHS: $(df -Pk /tmp /work /mnt/run /root 2>/dev/null | tail -n +2 \\
  | awk '{{print $6 "=" $4}}' | tr '\\n' ' ')"
mkdir -p /tmp/s15
start=$(date +%s)
dd if=/dev/zero of=/tmp/s15/fill bs=64M count={blocks} conv=fsync 2>/tmp/s15/dd.err
echo "DD_EXIT: $?"
echo "DD_SECONDS: $(( $(date +%s) - start ))"
echo "FILLED_BYTES: $(stat -c %s /tmp/s15/fill 2>/dev/null || echo 0)"
echo "DD_ERROR: $(grep -v records /tmp/s15/dd.err | head -n 2 | tr '\\n' ' ')"
echo "DF_TMP_FULL: $(df -Pk /tmp | tail -n 1 | awk '{{print $4}}')"
rm -f /tmp/s15/fill
echo "DF_TMP_AFTER: $(df -Pk /tmp | tail -n 1 | awk '{{print $4}}')"
sleep {sleep_s}
timeout {burn_s} sh -c 'while :; do :; done'
exit 0
"""


def default_image() -> str | None:
    data = yaml.safe_load((ROOT / "config" / "sandbox_images.yaml").read_text("utf-8")) or {}
    images = list((data.get("images") or {}).values())
    return str(images[-1]) if images else None


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--image", help="the imported runner-cpu UUID (default: the last one)")
    parser.add_argument("--cap-gib", type=int, default=48)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--sleep", type=int, default=0, help="seconds to idle before exiting")
    parser.add_argument("--burn", type=int, default=0, help="seconds of one busy CPU")
    args = parser.parse_args()

    env = require_env("NEBIUS_API_KEY", "TF_PROJECT_ID")
    sbx = Sandboxes(args.base_url, env["NEBIUS_API_KEY"], env["TF_PROJECT_ID"])
    r = Report()
    image = args.image or default_image()
    if not image:
        r.fail("no image: pass --image")
        r.summary()
        print("VERDICT: FAIL")
        return 1
    r.fact(f"image: {image}")

    print("== disk probe (networking disabled, disposable)")
    op_id, err = sbx.spawn(
        {
            "image": image,
            "command": probe(args.cap_gib, args.sleep, args.burn),
            "shell": True,
            "uid": 0,
            "gid": 0,
            "cwd": "/tmp",  # noqa: S108 - inside the disposable sandbox
            "networking": {"enabled": False},
            "disposable": True,
            "timeout": args.timeout,
            "truncate_output_at": 1024 * 1024,
        }
    )
    if op_id is None:
        r.fail(err)
        r.summary()
        print("VERDICT: FAIL")
        return 1
    op, took = sbx.wait(op_id, args.timeout + 120)
    result = (op.get("metadata") or {}).get("result") or {}
    stdout = decode(result.get("stdout"))
    error = str(op.get("error") or "")
    code = (result.get("state") or {}).get("exit_code")  # where SandboxesJobBackend reads it
    r.fact(f"status {op.get('status')} in {took} s, exit_code {code!r}")
    r.fact(f"error {error[:300]!r} (matches A-51's classifier: {bool(DISK_FULL.search(error))})")
    r.fact(f"resources {result.get('resources')}")
    table = stdout.partition("DF_ALL:\n")[2].partition("DF_PATHS:")[0].strip()
    print("-- df -PkT\n" + table)
    for key in ("DF_PATHS", "DD_EXIT", "DD_SECONDS", "FILLED_BYTES", "DD_ERROR"):
        r.fact(f"{key} = {line(stdout, key)}")
    r.fact(
        f"DF_TMP_FULL = {line(stdout, 'DF_TMP_FULL')} KiB, after rm {line(stdout, 'DF_TMP_AFTER')}"
    )
    filled = line(stdout, "FILLED_BYTES")
    if filled.isdigit():
        r.fact(f"the sandbox /tmp took {int(filled) / 2**30:.2f} GiB before dd stopped")
    dd_error = line(stdout, "DD_ERROR")
    if dd_error:
        r.fact(f"dd's error matches A-51's classifier: {bool(DISK_FULL.search(dd_error))}")
    measured = bool(table) and filled.isdigit() and op.get("status") not in (None, "PENDING")
    if not measured:
        r.fail(f"not measured; stderr {decode(result.get('stderr'))[:300]!r}")
    r.summary()
    print("VERDICT: " + ("PASS" if measured else "FAIL"))
    return 0 if measured else 1


if __name__ == "__main__":
    sys.exit(main())
