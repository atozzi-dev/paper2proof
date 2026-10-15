"""Deterministic resource names (invariant 9). Bucket and job names can't contain underscores,
so they are always built from the run slug."""

import re

from app.execution.backend import JobKind

_RUN_ID = re.compile(r"^r_([0-9a-z]{26})$")


def run_slug(run_id: str) -> str:
    match = _RUN_ID.fullmatch(run_id)
    if match is None:
        raise ValueError(f"not a run id: {run_id!r}")
    return match.group(1)


def job_name(kind: JobKind, run_id: str, attempt: int) -> str:
    """`p2p-<kind>-<slug>-<attempt>` (attempt is the iteration for smoke jobs)."""
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    return f"p2p-{kind.value}-{run_slug(run_id)}-{attempt}"


def bucket_name(run_id: str) -> str:
    return f"p2p-run-{run_slug(run_id)}"
