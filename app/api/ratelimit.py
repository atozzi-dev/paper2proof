"""In-memory token buckets (one instance, one process: the endpoint has a single replica)."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    def allow(self, scope: str, key: str, per_minute: int) -> bool:
        """Take one token from the (scope, key) bucket refilled at `per_minute` per 60 s."""
        now = self.clock()
        bucket = self._buckets.get((scope, key))
        if bucket is None:
            bucket = self._buckets[(scope, key)] = _Bucket(float(per_minute), now)
        bucket.tokens = min(
            float(per_minute), bucket.tokens + (now - bucket.updated) * per_minute / 60
        )
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True
