"""Spike S13: idle timeout of the managed endpoint URL on long SSE connections.

Run:   uv run python spikes/s13_sse_probe/client.py https://<endpoint-url> [--minutes 10]
Needs: the S6 probe endpoint still running (spikes/s6_endpoint.sh with KEEP=1). Free beyond
       the endpoint's own CPU time.

Opens three SSE streams in parallel against /sse with server event intervals of 15, 30 and 60 s
for --minutes each, and records when each stream ends and why.
PASS = the 15 s stream (our production heartbeat) survives the full duration.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spikelib import Report

INTERVALS = (15, 30, 60)


def stream(base: str, hb: int, duration: int, out: dict[int, str]) -> None:
    url = f"{base.rstrip('/')}/sse?hb={hb}&duration={duration}"
    start = time.monotonic()
    events, last_id, reason = 0, "", "closed by server"
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=hb + 60) as resp:  # noqa: S310
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("id:"):
                    events, last_id = events + 1, line[3:].strip()
                elif line == "event: done":
                    reason = "completed"
                    break
    except Exception as err:
        reason = f"{type(err).__name__}: {err}"
    elapsed = round(time.monotonic() - start)
    out[hb] = f"hb={hb}s lasted {elapsed}s, {events} events (last id {last_id or '-'}), {reason}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="endpoint base URL, e.g. https://xyz.endpoints.nebius.cloud")
    parser.add_argument("--minutes", type=float, default=10)
    args = parser.parse_args()

    duration = int(args.minutes * 60)
    results: dict[int, str] = {}
    threads = [
        threading.Thread(target=stream, args=(args.url, hb, duration, results)) for hb in INTERVALS
    ]
    print(f"== {len(threads)} streams for {duration}s against {args.url}")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    r = Report()
    for hb in INTERVALS:
        r.fact(results.get(hb, f"hb={hb}s: no result"))
    if "completed" not in results.get(15, ""):
        r.fail("15 s heartbeat stream did not survive: shorten the heartbeat or rely on polling")
    for hb in (30, 60):
        if "completed" not in results.get(hb, ""):
            r.partial(f"{hb} s interval cut: the idle timeout is between 15 and {hb} s")
    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
