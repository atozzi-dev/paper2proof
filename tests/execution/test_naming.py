import re

import pytest

from app.domain.ids import new_run_id
from app.execution.backend import JobKind
from app.execution.naming import bucket_name, job_name, run_slug

RUN = "r_01jb7z00000000000000000000"


def test_new_run_ids_are_ulids_and_sortable() -> None:
    ids = [new_run_id() for _ in range(50)]
    assert all(re.fullmatch(r"r_[0-9a-hjkmnp-tv-z]{26}", i) for i in ids)
    assert len(set(ids)) == 50
    prefixes = [i[2:12] for i in ids]  # 48-bit millisecond timestamp
    assert prefixes == sorted(prefixes)


def test_run_slug_strips_the_prefix() -> None:
    assert run_slug(RUN) == "01jb7z00000000000000000000"


@pytest.mark.parametrize(
    "bad", ["01jb7z00000000000000000000", "r_UPPERCASE0000000000000000", "r_x"]
)
def test_run_slug_rejects_malformed_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        run_slug(bad)


def test_names_are_deterministic_and_have_no_underscores() -> None:
    assert job_name(JobKind.EXEC, RUN, 2) == "p2p-exec-01jb7z00000000000000000000-2"
    assert job_name(JobKind.PROVISION, RUN, 1) == "p2p-prov-01jb7z00000000000000000000-1"
    assert job_name(JobKind.SMOKE, RUN, 7) == "p2p-smoke-01jb7z00000000000000000000-7"
    assert bucket_name(RUN) == "p2p-run-01jb7z00000000000000000000"
    for name in (job_name(JobKind.EXEC, RUN, 99), bucket_name(RUN)):
        assert "_" not in name
        assert len(name) <= 63
    assert job_name(JobKind.EXEC, RUN, 1) == job_name(JobKind.EXEC, RUN, 1)


def test_attempt_must_be_positive() -> None:
    with pytest.raises(ValueError):
        job_name(JobKind.EXEC, RUN, 0)
