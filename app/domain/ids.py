"""Run identifiers: `r_` + a lowercase ULID (48-bit ms timestamp + 80 random bits)."""

import secrets
import time

CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
RUN_ID_PATTERN = r"^r_[0-9a-hjkmnp-tv-z]{26}$"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(CROCKFORD[rem])
    return "".join(reversed(chars))


def new_run_id() -> str:
    millis = time.time_ns() // 1_000_000
    return "r_" + _encode(millis, 10) + _encode(secrets.randbits(80), 16)
