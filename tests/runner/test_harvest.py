"""Harvest of a Sandboxes job's outputs (images/runner/harvest.py, docs/specs/hybrid-deploy.md)."""

import base64
import importlib.util
import io
import os
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKER = "ab" * 16


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("p2p_harvest", ROOT / "images/runner/harvest.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["p2p_harvest"] = module
    spec.loader.exec_module(module)
    return module


harvest = _load()


def _members(out: str) -> dict[str, bytes]:
    start = f"\n{MARKER}\n"
    end = f"\n{MARKER}-END\n"
    assert out.startswith(start) and out.endswith(end)
    raw = base64.b64decode(out[len(start) : -len(end)], validate=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        found = {}
        for member in tar.getmembers():
            assert member.isreg()
            assert member.mode == 0o644 and member.mtime == 0
            extracted = tar.extractfile(member)
            assert extracted is not None
            found[member.name] = extracted.read()
    return found


def test_exec_harvest_keeps_only_the_allowed_files(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_bytes(b'{"ok": true}')
    (tmp_path / "env.json").write_bytes(b"{}")
    (tmp_path / "live.log").write_bytes(b"line\n")
    (tmp_path / "stdout.log").write_bytes(b"out")
    (tmp_path / "other.txt").write_bytes(b"not allowed")
    (tmp_path / "seeds").mkdir()
    out = harvest.harvest(tmp_path, "exec", MARKER)
    assert _members(out) == {
        "results.json": b'{"ok": true}',
        "env.json": b"{}",
        "live.log": b"line\n",
        "stdout.log": b"out",
    }


def test_smoke_harvest_is_result_json_only(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_bytes(b'{"nonce": "x"}')
    (tmp_path / "results.json").write_bytes(b"{}")
    assert _members(harvest.harvest(tmp_path, "smoke", MARKER)) == {
        "result.json": b'{"nonce": "x"}'
    }


def test_logs_keep_their_tail_and_json_keeps_one_byte_over_its_cap(tmp_path: Path) -> None:
    cap = harvest.CAPS["exec"]["stdout.log"]
    json_cap = harvest.CAPS["exec"]["results.json"]
    (tmp_path / "stdout.log").write_bytes(b"x" * 10 + b"y" * cap)
    (tmp_path / "results.json").write_bytes(b"{" * (json_cap + 50))
    found = _members(harvest.harvest(tmp_path, "exec", MARKER))
    assert found["stdout.log"] == b"y" * cap  # the end of a log is what explains a failure
    assert len(found["results.json"]) == json_cap + 1  # still "too large" for the reader


def test_a_missing_or_empty_directory_harvests_nothing(tmp_path: Path) -> None:
    assert _members(harvest.harvest(tmp_path / "missing", "exec", MARKER)) == {}
    assert _members(harvest.harvest(tmp_path, "exec", MARKER)) == {}


@pytest.mark.skipif(os.name == "nt", reason="symlinks and FIFOs need POSIX")
def test_links_fifos_and_directories_are_never_followed(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"root only")
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.json").symlink_to(secret)
    os.mkfifo(out / "env.json")
    (out / "live.log").mkdir()
    assert _members(harvest.harvest(out, "exec", MARKER)) == {}
    link = tmp_path / "linked"
    link.symlink_to(out, target_is_directory=True)
    (out / "stdout.log").write_bytes(b"real")
    assert _members(harvest.harvest(link, "exec", MARKER)) == {}  # not even the directory


@pytest.mark.parametrize("marker", ["", "short", "zz" * 16, "ab" * 40, "AB" * 16])
def test_the_marker_must_be_lowercase_hex(tmp_path: Path, marker: str) -> None:
    with pytest.raises(ValueError, match="marker"):
        harvest.harvest(tmp_path, "exec", marker)


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        harvest.harvest(tmp_path, "prov", MARKER)


def test_main_prints_the_harvest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "result.json").write_bytes(b"{}")
    code = harvest.main(["--dir", str(tmp_path), "--kind", "smoke", "--marker", MARKER])
    assert code == 0
    assert _members(capsys.readouterr().out) == {"result.json": b"{}"}


def test_main_refuses_a_bad_marker(tmp_path: Path) -> None:
    assert harvest.main(["--dir", str(tmp_path), "--kind", "smoke", "--marker", "nope"]) == 2


def test_the_control_plane_parses_what_the_image_sends(tmp_path: Path) -> None:
    from app.execution.sandbox_jobs import CAPS, parse_harvest

    assert CAPS == harvest.CAPS  # one contract, two copies (the image has no app package)
    (tmp_path / "results.json").write_bytes(b'{"ok": 1}')
    (tmp_path / "stderr.log").write_bytes(b"warn")
    text = "runner noise\n" + harvest.harvest(tmp_path, "exec", MARKER)
    expected = {"results.json": b'{"ok": 1}', "stderr.log": b"warn"}
    assert parse_harvest(text, MARKER, "exec") == expected
