"""Secret scan for bundle files (invariant 4). Pure functions: they run in the CPU pool."""

import json
import re
from collections.abc import Sequence

from pydantic import SecretStr

from app.settings import Settings

REDACTED = b"[REDACTED]"
MIN_KNOWN_LEN = 8  # shorter "secrets" would redact ordinary words
PATTERNS = re.compile(
    rb"tvly-[A-Za-z0-9_-]{8,}"
    rb"|Bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|AKIA[0-9A-Z]{16}"
    rb"|hf_[A-Za-z0-9]{20,}"
    rb"|sk-[A-Za-z0-9_-]{20,}"
    rb"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
    rb"|xox[abposr]-[A-Za-z0-9-]{10,}"
    rb"|AIza[0-9A-Za-z_-]{35}"
    rb"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    # Nebius IAM access tokens (`nebius iam get-access-token`, the SDK, the registry password):
    # "v1." and a long base64url payload, optionally more dot-separated parts
    rb"|(?<![A-Za-z0-9_.-])v1\.[A-Za-z0-9_-]{40,}(?:\.[A-Za-z0-9_-]+)*"
)


def secret_values(settings: Settings) -> tuple[str, ...]:
    """The control plane's own secret values, to find them verbatim in bundle files."""
    values = (getattr(settings, name) for name in type(settings).model_fields)
    return tuple(
        v.get_secret_value()
        for v in values
        if isinstance(v, SecretStr) and len(v.get_secret_value()) >= MIN_KNOWN_LEN
    )


def _forms(value: bytes) -> set[bytes]:
    """The value as is and as it appears inside a JSON string."""
    escaped = json.dumps(value.decode("utf-8", "replace"))[1:-1].encode()
    return {value, escaped}


def known_pattern(known: Sequence[bytes]) -> re.Pattern[bytes] | None:
    forms = {f for k in known if len(k) >= MIN_KNOWN_LEN for f in _forms(k)}
    usable = sorted(forms, key=len, reverse=True)
    return re.compile(b"|".join(re.escape(k) for k in usable)) if usable else None


def find_secrets(data: bytes, known: Sequence[bytes] = ()) -> bool:
    pattern = known_pattern(known)
    return bool(PATTERNS.search(data) or (pattern and pattern.search(data)))


def redact(data: bytes, known: Sequence[bytes] = ()) -> tuple[bytes, bool]:
    """Replace every match with [REDACTED]; also says whether anything changed."""
    cleaned = data
    pattern = known_pattern(known)
    if pattern is not None:
        cleaned = pattern.sub(REDACTED, cleaned)
    cleaned = PATTERNS.sub(REDACTED, cleaned)
    return cleaned, cleaned != data
