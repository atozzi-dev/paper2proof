"""The API rate limiter keeps client addresses in memory only, and not longer than it needs."""

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
