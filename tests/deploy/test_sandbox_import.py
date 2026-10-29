"""scripts/sandbox_import.py against a respx mock of the Sandboxes API: nothing leaves the test."""

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
import yaml

from app.execution.smoke_factory import load_sandbox_image

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "c" * 64
IMAGE = f"cr.eu-north1.nebius.cloud/e00registry/p2p-runner-cpu@{DIGEST}"
UUID = "11111111-2222-3333-4444-555555555555"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sandbox_import", ROOT / "scripts/sandbox_import.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_import"] = module
    spec.loader.exec_module(module)
    return module


tool = _load()
API = tool.API


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TF_PROJECT_ID", "project-e00test")
    monkeypatch.setattr(tool.time, "sleep", lambda s: None)


def _mock(
    router: respx.Router,
    *,
    check_code: int = 0,
    import_status: str = "SUCCESS",
    stdout: str = "uid 10001\ntorch 2.5\n",
) -> list[Any]:
    bodies: list[Any] = []

    def imported(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"uuid": "op-import"})

    def spawned(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"uuid": "op-check"})

    router.post(f"{API}/v1/images/import").mock(side_effect=imported)
    router.post(f"{API}/v1/instances").mock(side_effect=spawned)
    router.get(f"{API}/v1/operations/op-import").mock(
        side_effect=[
            httpx.Response(200, json={"status": "EXECUTING"}),
            httpx.Response(200, json={"status": import_status, "result": {"image": UUID}}),
        ]
    )
    result = {
        "state": {"exit_code": check_code},
        "stdout": {"value": base64.b64encode(stdout.encode()).decode(), "encoding": "base64"},
    }
    router.get(f"{API}/v1/operations/op-check").mock(
        return_value=httpx.Response(200, json={"status": "SUCCESS", "metadata": {"result": result}})
    )
    return bodies


def test_an_image_is_imported_checked_offline_and_recorded(tmp_path: Path) -> None:
    config = tmp_path / "sandbox_images.yaml"
    config.write_text("images:\n  sha256:" + "a" * 64 + ": old-uuid\n", encoding="utf-8")
    with respx.mock as router:
        bodies = _mock(router)
        assert tool.main(["--image", IMAGE, "--config", str(config)]) == 0
    imported, check = bodies
    assert imported["registry"] == {"url": f"docker://{IMAGE}"}
    assert check["image"] == UUID and check["networking"] == {"enabled": False}
    assert (check["uid"], check["gid"]) == (0, 0)  # as the job command runs
    assert load_sandbox_image(config, IMAGE) == UUID
    assert (
        yaml.safe_load(config.read_text(encoding="utf-8"))["images"]["sha256:" + "a" * 64]
        == "old-uuid"
    )


def test_registry_credentials_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("P2P_REGISTRY_PASSWORD", "iam-token-not-real")
    with respx.mock as router:
        bodies = _mock(router)
        argv = ["--image", IMAGE, "--username", "iam", "--config", str(tmp_path / "i.yaml")]
        assert tool.main(argv) == 0
    assert bodies[0]["registry"]["credentials"] == {
        "username": "iam",
        "password": "iam-token-not-real",
    }
    assert "iam-token-not-real" not in capsys.readouterr().out
    assert "iam-token-not-real" not in (tmp_path / "i.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "argv",
    [
        ["--image", "cr.example/p2p-runner-cpu:latest"],  # a tag, not a digest (invariant 8)
        ["--image", IMAGE, "--username", "iam"],  # no P2P_REGISTRY_PASSWORD
    ],
)
def test_bad_arguments_stop_before_any_call(tmp_path: Path, argv: list[str]) -> None:
    with respx.mock as router:
        assert tool.main([*argv, "--config", str(tmp_path / "i.yaml")]) == 2
        assert not router.calls
    assert not (tmp_path / "i.yaml").exists()


def test_missing_credentials_stop_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TF_PROJECT_ID")
    assert tool.main(["--image", IMAGE, "--config", str(tmp_path / "i.yaml")]) == 2


def test_an_image_whose_uid_drop_fails_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "i.yaml"
    with respx.mock as router:
        _mock(router, stdout="uid \ntorch 2.5\n")
        assert tool.main(["--image", IMAGE, "--config", str(config)]) == 1
    assert not config.exists()


@pytest.mark.parametrize(("check_code", "status"), [(1, "SUCCESS"), (0, "FAILED")])
def test_a_failed_import_or_check_records_nothing(
    tmp_path: Path, check_code: int, status: str
) -> None:
    config = tmp_path / "i.yaml"
    with respx.mock as router:
        _mock(router, check_code=check_code, import_status=status)
        assert tool.main(["--image", IMAGE, "--config", str(config)]) == 1
    assert not config.exists()


def test_an_index_is_imported_by_its_tag_and_recorded_by_its_digest(tmp_path: Path) -> None:
    """The Sandboxes importer answers NAME_UNKNOWN for an OCI index pulled by digest and
    accepts its tag (2026-10-29): the tag is checked against the digest beforehand."""
    config = tmp_path / "i.yaml"
    with respx.mock as router:
        bodies = _mock(router)
        argv = ["--image", IMAGE, "--pull-tag", "cf50c98", "--config", str(config)]
        assert tool.main(argv) == 0
    url = IMAGE.split("@")[0] + ":cf50c98"
    assert bodies[0]["registry"] == {"url": f"docker://{url}"}
    assert load_sandbox_image(config, IMAGE) == UUID  # keyed by the index digest


@pytest.mark.parametrize("tag", ["", "a/b", ".x", "x" * 200])
def test_a_bad_tag_stops_before_any_call(tmp_path: Path, tag: str) -> None:
    with respx.mock as router:
        argv = ["--image", IMAGE, "--pull-tag", tag, "--config", str(tmp_path / "i.yaml")]
        assert tool.main(argv) == 2
        assert not router.calls


def test_the_offline_check_loads_the_openmp_runtime() -> None:
    """The last gate before a digest gets a UUID: it accepted the image whose LightGBM and
    XGBoost could not load libgomp.so.1 (production, 2026-10-29)."""
    assert "libgomp.so.1" in tool.CHECK
