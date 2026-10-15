"""Network probe injected into spike jobs (S5, S10, S11). Runs INSIDE the job container.

Reads /etc/p2p/probe.json:
  {"mount": "/mnt/run" | null,
   "tcp":   [["host", port], ...],     # plain TCP connect
   "tls":   [["host", port], ...]}     # TCP + TLS handshake with SNI = host
Prints one line per check (MOUNT_*, TCP <host>:<port> OPEN|REFUSED|TIMEOUT|ERROR, TLS ...).
Standard library only; never prints environment variables.
"""

import json
import socket
import ssl
import time
from pathlib import Path

SPEC = Path("/etc/p2p/probe.json")


def tcp(host: str, port: int, timeout: float = 4.0) -> str:
    try:
        with socket.create_connection((host, port), timeout):
            return "OPEN"
    except ConnectionRefusedError:
        return "REFUSED"
    except TimeoutError:
        return "TIMEOUT"
    except OSError as err:
        return f"ERROR({type(err).__name__}: {err})"


def tls(host: str, port: int, timeout: float = 6.0) -> str:
    ctx = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout) as raw,
            ctx.wrap_socket(raw, server_hostname=host) as conn,
        ):
            return f"OK({conn.version()})"
    except ssl.SSLError as err:
        return f"TLS_ERROR({err.reason})"
    except TimeoutError:
        return "TIMEOUT"
    except OSError as err:
        return f"ERROR({type(err).__name__}: {err})"


def mount(path: str) -> str:
    target = Path(path) / f"probe-{int(time.time())}.txt"
    try:
        target.write_text("ok\n", encoding="utf-8")
        content = target.read_text(encoding="utf-8").strip()
        listing = sorted(p.name for p in Path(path).iterdir())[:10]
        target.unlink()  # leave the bucket empty so it can be deleted afterwards
        return f"MOUNT_OK wrote+deleted={target.name} read={content} ls={listing}"
    except OSError as err:
        return f"MOUNT_FAIL {type(err).__name__}: {err}"


def main() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    print(f"PROBE_START {socket.gethostname()}", flush=True)
    if spec.get("mount"):
        print(mount(spec["mount"]), flush=True)
    for host, port in spec.get("tcp", []):
        print(f"TCP {host}:{port} {tcp(host, int(port))}", flush=True)
    for host, port in spec.get("tls", []):
        print(f"TLS {host}:{port} {tls(host, int(port))}", flush=True)
    print("PROBE_END", flush=True)


if __name__ == "__main__":
    main()
