"""The API rate limiter keeps client addresses in memory only, and not longer than it needs."""

import itertools
import sys
import threading

from app.api.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_an_idle_address_is_forgotten_after_a_minute() -> None:
    """The privacy notice (web/src/pages/Privacy.tsx) promises it: an address idle for a minute
    is dropped even when the table is far from full."""
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    assert limiter.allow("api", "203.0.113.7", 60)
    clock.now = 61.0
    assert limiter.allow("api", "198.51.100.2", 60)
    assert {key for _, key in limiter._buckets} == {"198.51.100.2"}


def test_an_active_address_keeps_its_bucket() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for second in range(5):
        clock.now = float(second)
        assert limiter.allow("session", "203.0.113.7", 5)
    clock.now = 5.0
    assert not limiter.allow("session", "203.0.113.7", 5)  # still limited: not forgotten


def test_the_minute_prune_keeps_a_drained_bucket() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    clock.now = 59.0
    for _ in range(5):
        assert limiter.allow("session", "203.0.113.7", 5)
    clock.now = 60.0
    assert limiter.allow("api", "198.51.100.2", 60)  # this call prunes
    assert not limiter.allow("session", "203.0.113.7", 5)  # still drained


def test_threads_can_share_the_limiter() -> None:
    """FastAPI runs the sync rate-limit dependencies in its thread pool while the session
    endpoint uses the limiter on the event loop: a prune must not race an insert."""
    counter = itertools.count()
    limiter = RateLimiter(clock=lambda: float(next(counter)) * 61)  # every call prunes
    errors: list[BaseException] = []

    def hammer(worker: int) -> None:
        try:
            for i in range(3000):
                limiter.allow("api", f"{worker}-{i}", 60)
        except BaseException as err:  # collected for the assert below
            errors.append(err)

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert errors == []
