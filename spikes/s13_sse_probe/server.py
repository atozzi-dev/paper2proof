"""S6/S13 probe server, deployed on a Serverless Endpoint. Standard library only, no build step.

Usage (inside the container): python server.py [--port 8080] [--data /data]   (--help for help)

Endpoints:
  GET /healthz      -> {"ok": true}
  GET /boot         -> boot id, start time, image info and whether the marker file survived a
                       restart (S6: is the container disk ephemeral?)
  GET /sse?hb=15&duration=600
                    -> Server-Sent Events: an `id:`-numbered `tick` event every hb seconds and a
                       `: hb` comment in between, for `duration` seconds
                       (max 900 s, at most 8 concurrent streams; S13 idle-timeout test).
The server itself has no verdict; the client and s6_endpoint.sh print `VERDICT: ...`.
"""

from __future__ import annotations

import argparse
import json
import platform
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BOOT_ID = uuid.uuid4().hex[:12]
STARTED = time.time()
STATE: dict[str, object] = {}
# Public, unauthenticated throwaway endpoint: bound every knob a stranger could turn.
MAX_STREAMS = 8
MAX_DURATION_S = 900
STREAMS = threading.BoundedSemaphore(MAX_STREAMS)


def parse_sse_params(query: dict[str, list[str]]) -> tuple[int, int] | None:
    """Clamp hb to [1, 300] s and duration to [1, MAX_DURATION_S] s; None on non-integers."""
    try:
        hb = int(query.get("hb", ["15"])[0])
        duration = int(query.get("duration", ["600"])[0])
    except ValueError:
        return None
    return max(1, min(300, hb)), max(1, min(MAX_DURATION_S, duration))


def init_marker(data_dir: Path) -> None:
    marker = data_dir / "p2p-s6-marker.json"
    STATE["marker_path"] = str(marker)
    STATE["marker_existed_at_boot"] = marker.exists()
    if marker.exists():
        STATE["marker_content"] = marker.read_text(encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"boot_id": BOOT_ID, "at": STARTED}), encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # drop connections that never send a full request (each holds a thread)

    def _json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/healthz":
            self._json({"ok": True})
        elif url.path == "/boot":
            os_release = Path("/etc/os-release")
            self._json(
                {
                    "boot_id": BOOT_ID,
                    "started": STARTED,
                    "uptime_s": round(time.time() - STARTED, 1),
                    "python": platform.python_version(),
                    "os": os_release.read_text().splitlines()[:2] if os_release.exists() else [],
                    **STATE,
                }
            )
        elif url.path == "/sse":
            self._sse(parse_qs(url.query))
        else:
            self.send_error(404)

    def _sse(self, query: dict[str, list[str]]) -> None:
        params = parse_sse_params(query)
        if params is None:
            self.send_error(400, "hb and duration must be integers")
            return
        if not STREAMS.acquire(blocking=False):
            self.send_error(503, "too many streams")
            return
        try:
            self._stream(*params)
        finally:
            STREAMS.release()

    def _stream(self, hb: int, duration: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        start, event_id = time.time(), 0
        try:
            while time.time() - start < duration:
                event_id += 1
                data = json.dumps({"t": round(time.time() - start)})
                msg = f"id: {event_id}\nevent: tick\ndata: {data}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
                time.sleep(hb)
            self.wfile.write(b"event: done\ndata: {}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="S6/S13 probe server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default="/data", help="directory for the restart marker")
    args = parser.parse_args()
    init_marker(Path(args.data))
    print(f"probe server boot_id={BOOT_ID} port={args.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
