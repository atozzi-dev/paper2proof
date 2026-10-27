"""The hybrid deploy (docs/specs/hybrid-deploy.md): the control-plane host runs only the
provision and fetch jobs (our provisioner image) in local docker, for any paper; model and
paper code never runs here (invariant 1)."""

from pathlib import Path

import pytest

from app.execution.backend import JobKind
from app.execution.dev_docker import DevDockerBackend
from app.execution.local_trusted import LocalTrustedRefused
from tests.execution.test_dev_docker import RUN, SLUG, Acl, Docker, _run_argv, _spec

PROVISIONER = "cr.eu-north1.nebius.cloud/e00abc/p2p-provisioner@sha256:" + "b" * 64


async def _no_paper(run_id: str) -> None:
    return None  # not a canary: these jobs do not need one


def _local(tmp_path: Path, docker: Docker, env: str = "prod") -> DevDockerBackend:
    return DevDockerBackend(
        env=env, canaries={}, paper=_no_paper, root=tmp_path, docker=docker,
        host_uid=None, acl=Acl(), provisioner_only=PROVISIONER,
    )  # fmt: skip


def _prov(kind: JobKind = JobKind.PROVISION, **changes: object):  # type: ignore[no-untyped-def]
    short = "prov" if kind is JobKind.PROVISION else kind.value
    fields: dict[str, object] = {"name": f"p2p-{short}-{SLUG}-1", "image_digest_ref": PROVISIONER}
    if kind is JobKind.FETCH:
        fields["volumes"] = [v for v in _spec().volumes if v.mount_path == "/mnt/run"]
    return _spec(kind, **{**fields, **changes})


async def test_provision_and_fetch_run_for_any_paper_by_registry_digest(tmp_path: Path) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    await backend.ensure_run_bucket(RUN)
    await backend.submit(_prov())
    run = _run_argv(docker)
    assert run[-1] == PROVISIONER  # pulled and run by its registry digest (invariant 8)
    assert "--network=none" not in run and "--read-only" in run and "--cap-drop=ALL" in run
    await backend.submit(_prov(JobKind.FETCH))


@pytest.mark.parametrize("kind", [JobKind.EXEC, JobKind.SMOKE])
async def test_model_code_never_runs_on_this_host(tmp_path: Path, kind: JobKind) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    with pytest.raises(LocalTrustedRefused, match="provision and fetch"):
        await backend.submit(_spec(kind, image_digest_ref=PROVISIONER))
    assert not [c for c in docker.calls if c[1] == "run"]


async def test_only_the_configured_provisioner_image_runs(tmp_path: Path) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    other = PROVISIONER.replace("b" * 64, "c" * 64)
    with pytest.raises(LocalTrustedRefused, match="provisioner image"):
        await backend.submit(_prov(image_digest_ref=other))
    assert not [c for c in docker.calls if c[1] == "run"]


def test_the_provisioner_only_backend_is_for_the_hybrid_prod_deploy(tmp_path: Path) -> None:
    for env in ("dev", "test"):
        with pytest.raises(LocalTrustedRefused, match="hybrid"):
            _local(tmp_path, Docker(), env=env)


async def test_a_resumed_provision_job_is_found_by_name(tmp_path: Path) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    first = await backend.submit(_prov())
    assert await backend.submit(_prov()) == first
    assert len([c for c in docker.calls if c[1] == "run"]) == 1
