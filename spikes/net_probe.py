"""Network probe injected into spike jobs (S5, S10, S11). Runs INSIDE the job container.

Reads /etc/p2p/probe.json:
  {"mount": "/mnt/run" | null,
   "tcp":   [["host", port], ...],     # plain TCP connect
   "tls":   [["host", port], ...],     # TCP + TLS handshake with SNI = host
   "dns":   ["name", ...],             # does the job resolve public names (a DNS tunnel)?
   "meta":  true}                      # GET the instance metadata service (169.254.169.254)
Prints one line per check (MOUNT_*, TCP <host>:<port> OPEN|REFUSED|TIMEOUT|ERROR, TLS ...,
DNS <name> RESOLVED(n)|FAILED, META OPEN(status, token=yes|no)|<error>), then PROBE_END.
Standard library only; never prints environment variables or a metadata body.
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
    except ConnectionRefusedError:
        return "REFUSED"
    except ssl.SSLError as err:
        return f"TLS_ERROR({err.reason})"
    except TimeoutError:
        return "TIMEOUT"
    except OSError as err:
        return f"ERROR({type(err).__name__}: {err})"


def dns(name: str) -> str:
    try:
        return f"RESOLVED({len(socket.getaddrinfo(name, 443))})"
    except OSError as err:
        return f"FAILED({type(err).__name__})"


# The root, and the credential paths of the common metadata services: only status codes are
# printed, never a body (a token must not reach the job logs).
META_PATHS = {
    "root": "/",
    "token-path": "/computeMetadata/v1/instance/service-accounts/default/token",
    "credentials-path": "/latest/meta-data/iam/security-credentials/",
}


def http_status(path: str, timeout: float = 4.0) -> str:
    request = (
        f"GET {path} HTTP/1.0\r\nHost: 169.254.169.254\r\n"
        "Metadata-Flavor: Google\r\nMetadata: true\r\n\r\n"
    )
    try:
        with socket.create_connection(("169.254.169.254", 80), timeout) as conn:
            conn.sendall(request.encode())
            head = conn.recv(256).split(b"\r\n", 1)[0].split()
    except ConnectionRefusedError:
        return "REFUSED"
    except TimeoutError:
        return "TIMEOUT"
    except OSError as err:
        return f"ERROR({type(err).__name__})"
    return head[1].decode() if len(head) > 1 and head[1].isdigit() else "NO_STATUS"


def metadata() -> str:
    return " ".join(f"{name}={http_status(path)}" for name, path in META_PATHS.items())


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
    for name in spec.get("dns", []):
        print(f"DNS {name} {dns(name)}", flush=True)
    if spec.get("meta"):
        print(f"META 169.254.169.254:80 {metadata()}", flush=True)
    print("PROBE_END", flush=True)


if __name__ == "__main__":
    main()
