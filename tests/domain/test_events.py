from typing import get_args

from app.domain.events import JobKindName, JobLogPayload, JobStatusPayload
from app.execution.backend import JobKind


def test_job_kind_names_are_exactly_the_job_kinds() -> None:
    assert set(get_args(JobKindName)) == {kind.value for kind in JobKind}


def test_the_fetch_job_has_its_own_event_kind() -> None:
    status = JobStatusPayload(kind="fetch", name="p2p-fetch-x-1", state="RUNNING")
    assert status.kind == JobKind.FETCH.value
    assert JobLogPayload(kind="fetch", lines=["ok"]).kind == "fetch"
