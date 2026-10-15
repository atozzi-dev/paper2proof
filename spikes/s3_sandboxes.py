"""Spike S3: Token Factory Sandboxes access, effective limits, and an offline networking check.

Run:   uv run python spikes/s3_sandboxes.py [--image UUID|tag:NAME[:TAG]]
Needs: NEBIUS_API_KEY and TF_PROJECT_ID (sent as the `Project` header). Sandboxes beta access.
Cost:  two short disposable operations (seconds of CPU).

1. GET /v1/whoami -> permissions and limits (printed, token id omitted).
2. Picks a public image (prefers one tagged python, else ubuntu/busybox) unless --image is given.
3. Runs the same probe twice, disposable: networking enabled, then networking.enabled=false.
   The probe lists /sys/class/net and tries socket.create_connection(("pypi.org", 443), 3).
PASS = whoami OK, online run connects, offline run fails to connect and has only `lo`.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spikelib import Report, http, require_env

DEFAULT_BASE = "https://api.tokenfactory.nebius.com/sandboxes"
NON_TERMINAL = {"PENDING", "ASSIGNED", "EXECUTING"}
PROBE = r"""
echo "IFACES: $(ls /sys/class/net | tr '\n' ' ')"
if command -v python3 >/dev/null 2>&1; then
  python3 -c "import socket; socket.create_connection(('pypi.org', 443), 3); print('CONNECTED')" \
    2>/dev/null || echo CONNECT_FAILED
elif command -v wget >/dev/null 2>&1; then
  wget -q -T 3 -O /dev/null https://pypi.org/ && echo CONNECTED || echo CONNECT_FAILED
else
  echo CONNECT_UNTESTED
fi
"""


class Sandboxes:
    def __init__(self, base: str, key: str, project: str) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {key}", "Project": project}

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return http(method, self.base + path, headers=self.headers, body=body, timeout=120)

    def run(self, image: str, networking: bool, timeout_s: int = 60) -> dict[str, Any]:
        spec = {
            "image": image,
            "command": PROBE,
            "shell": True,
            "networking": {"enabled": networking},
            "disposable": True,
            "timeout": timeout_s,
        }
        start = time.monotonic()
        resp = self.call("POST", "/v1/instances", spec)
        if resp.status not in (200, 201):
            return {"error": f"spawn HTTP {resp.status} {resp.text(300)!r}"}
        op_id = (resp.json() or {}).get("uuid")
        deadline = time.monotonic() + timeout_s + 120
        op: Any = {}
        while time.monotonic() < deadline:
            op = self.call("GET", f"/v1/operations/{op_id}").json() or {}
            if op.get("status") not in NON_TERMINAL:
                break
            time.sleep(1)
        result = (op.get("metadata") or {}).get("result") or {}
        return {
            "status": op.get("status"),
            "exit_code": (result.get("state") or {}).get("exit_code"),
            "stdout": decode(result.get("stdout")),
            "stderr": decode(result.get("stderr"))[:300],
            "cost": op.get("cost") or (op.get("metadata") or {}).get("cost"),
            "seconds": round(time.monotonic() - start, 1),
        }


def decode(stream: Any) -> str:
    if not isinstance(stream, dict):
        return ""
    value = str(stream.get("value") or "")
    if stream.get("encoding") == "base64":
        value = base64.b64decode(value).decode("utf-8", errors="replace")
    return value


def pick_image(sbx: Sandboxes) -> str | None:
    for prefix in ("python", "ubuntu", "busybox"):
        images = (sbx.call("GET", f"/v1/images?tag={prefix}&limit=20").json() or {}).get(
            "images", []
        )
        if images:
            return str(images[0]["uuid"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--image", help="image UUID or tag:<name>[:<tag>]")
    args = parser.parse_args()

    env = require_env("NEBIUS_API_KEY", "TF_PROJECT_ID")
    sbx = Sandboxes(args.base_url, env["NEBIUS_API_KEY"], env["TF_PROJECT_ID"])
    r = Report()

    print("== GET /v1/whoami")
    who = sbx.call("GET", "/v1/whoami")
    if who.status != 200:
        r.fail(f"whoami HTTP {who.status} {who.text(300)!r} (beta access missing?)")
        r.summary()
        print("VERDICT: FAIL")
        return 1
    info = who.json() or {}
    r.fact(f"permissions: {info.get('permissions')}")
    r.fact(f"limits: {info.get('limits')}")
    r.fact(f"token_expiration: {info.get('token_expiration')}")

    image = args.image or pick_image(sbx)
    if not image:
        r.fail("no public image found; pass --image")
        r.summary()
        print("VERDICT: FAIL")
        return 1
    r.fact(f"image: {image}")

    print("== networking enabled (control run)")
    online = sbx.run(image, networking=True)
    r.fact(f"online: {online}")
    print("== networking disabled")
    offline = sbx.run(image, networking=False)
    r.fact(f"offline: {offline}")

    if "error" in online or "error" in offline:
        r.fail("spawn failed (see above)")
    else:
        if "CONNECT_UNTESTED" in online["stdout"]:
            r.partial("image has neither python3 nor wget; pass --image with python3")
        elif "CONNECTED" not in online["stdout"].replace("CONNECT_FAILED", ""):
            r.partial("control run could not connect either: offline result is not conclusive")
        if "CONNECTED" in offline["stdout"].replace("CONNECT_FAILED", ""):
            r.fail("networking.enabled=false still reached pypi.org")
        ifaces = offline["stdout"].split("IFACES:")[-1].split("\n")[0].split()
        if ifaces and set(ifaces) != {"lo"}:
            r.fail(f"offline run has interfaces {ifaces}, expected only lo")
        r.fact(f"latency: online {online['seconds']} s, offline {offline['seconds']} s")

    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
