"""images/api/start.sh with stubbed litestream and alembic (A-14). The task names bats; the same
checks run here under pytest + sh, so they need no extra tool on any machine."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "images" / "api" / "start.sh"
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALLS"
echo "env P2P_REPLICATION=${P2P_REPLICATION:-} key=${LITESTREAM_ACCESS_KEY_ID:-}" >> "$CALLS"
if [ -n "${FAIL_ON:-}" ] && [ "$(basename "$0") $1" = "$FAIL_ON" ]; then exit 1; fi
exit 0
"""


def _run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("litestream", "alembic"):
        stub = bin_dir / name
        stub.write_text(STUB, encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    calls = tmp_path / "calls.log"
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "P2P_STATE_BUCKET": "p2p-state",
        "S3_ENDPOINT": "https://storage.example",
        "RUNTIME_S3_KEY_ID": "AKEXAMPLE",
        "RUNTIME_S3_SECRET": "very-secret-value",
        "P2P_LITESTREAM_CONFIG": str(ROOT / "images" / "api" / "litestream.yml"),
    }
    assert SH is not None
    done = subprocess.run(  # noqa: S603 - the repo's own script with a test environment
        [SH, str(START)],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines


def test_restore_runs_before_migrations_then_uvicorn_under_litestream(tmp_path: Path) -> None:
    done, calls = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    commands = [c for c in calls if not c.startswith("env ")]
    assert commands[0].startswith("litestream restore ")
    assert "-if-db-not-exists" in commands[0]
    assert "-if-replica-exists" not in commands[0]  # a missing replica is an error
    assert commands[0].endswith("/var/lib/p2p/p2p.db")
    assert commands[1] == "alembic upgrade head"
    assert commands[2].startswith("litestream replicate ")
    assert commands[2].endswith(
        "-exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --no-server-header"
        " --timeout-graceful-shutdown 10"
    )
    assert len(commands) == 3


def test_the_app_knows_it_runs_under_replication(tmp_path: Path) -> None:
    _, calls = _run(tmp_path)
    environments = [c for c in calls if c.startswith("env ")]
    assert environments[-1].startswith("env P2P_REPLICATION=litestream ")


def test_s3_credentials_reach_litestream_without_being_printed(tmp_path: Path) -> None:
    done, calls = _run(tmp_path)
    assert all(c.endswith("key=AKEXAMPLE") for c in calls if c.startswith("env "))
    assert "very-secret-value" not in done.stdout + done.stderr


@pytest.mark.parametrize("failing", ["litestream restore", "alembic upgrade"])
def test_a_failed_step_stops_the_start(tmp_path: Path, failing: str) -> None:
    done, calls = _run(tmp_path, FAIL_ON=failing)
    assert done.returncode != 0
    assert not [c for c in calls if c.startswith("litestream replicate")]


@pytest.mark.parametrize(
    "missing", ["P2P_STATE_BUCKET", "S3_ENDPOINT", "RUNTIME_S3_KEY_ID", "RUNTIME_S3_SECRET"]
)
def test_missing_replication_settings_fail_fast(tmp_path: Path, missing: str) -> None:
    done, calls = _run(tmp_path, **{missing: ""})
    assert done.returncode != 0
    assert calls == []
    assert missing in done.stderr


def test_only_the_first_deploy_may_start_from_an_empty_database(tmp_path: Path) -> None:
    _, calls = _run(tmp_path, P2P_DB_BOOTSTRAP="1")
    assert "-if-replica-exists" in calls[0]


def test_a_plain_http_s3_endpoint_is_refused(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, S3_ENDPOINT="http://storage.example")
    assert done.returncode != 0 and calls == []


def test_forwarded_addresses_are_trusted_only_from_listed_proxies(tmp_path: Path) -> None:
    _, calls = _run(tmp_path, P2P_FORWARDED_ALLOW_IPS="10.0.0.0/8;192.168.1.2")
    assert calls[-2].endswith(" --proxy-headers --forwarded-allow-ips=10.0.0.0/8,192.168.1.2")


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "10.0.0.0/8 --reload",
        "$(id)",
        "0.0.0.0/0",
        "::/0",
        "10.0.0.0/8;0.0.0.0/0",
        "a,b",
        "0.0.0.0/00",  # still /0 for ipaddress, and so for uvicorn
        "::/00",
        "0.0.0.0/1;128.0.0.0/1",  # everyone, in two halves
        "10.0.0.0/7",
        "2001::/31",
        "10.0.0.0/8;",
        "999.0.0.1",
        "10.0.0.1/8",  # host bits set: uvicorn would quietly never match it
    ],
)
def test_a_wildcard_or_odd_proxy_list_is_refused(tmp_path: Path, value: str) -> None:
    done, calls = _run(tmp_path, P2P_FORWARDED_ALLOW_IPS=value)
    assert done.returncode != 0
    assert calls == []  # refused before the database is restored or migrated
