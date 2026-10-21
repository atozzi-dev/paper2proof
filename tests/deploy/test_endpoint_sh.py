"""deploy/endpoint.sh with stubbed nebius and curl (A-14): digests only, secrets by name only,
fail-closed lookups, inputs checked before anything changes, drain → stop → delete → create,
readiness of the new build, un-drain when a replace fails, one-shot bootstrap."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy._settings import ENV_LINES, REQUIRED

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "endpoint.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")
IMAGE = "cr.eu-north1.nebius.cloud/reg/p2p-api@sha256:" + "a" * 64
OLD = "cr.eu-north1.nebius.cloud/reg/p2p-api@sha256:" + "b" * 64

# nebius stub: `list` prints $LISTING (fails with $LIST_FAILS, or once the endpoint was deleted
# with $LIST_FAILS_AFTER_DELETE), `get` prints $ENDPOINT, `delete` empties the listing (fails
# with $DELETE_FAILS), `create` lists the new endpoint, `update` fails with $UPDATE_FAILS.
NEBIUS = """#!/bin/sh
echo "nebius $*" >> "$CALLS"
case "$1 $2 $3" in
  "ai endpoint list")
    [ -n "${LIST_FAILS:-}" ] && exit 1
    [ -n "${LIST_FAILS_AFTER_DELETE:-}" ] && [ -f "$CALLS.deleted" ] && exit 1
    cat "$LISTING" ;;
  "ai endpoint get") cat "$ENDPOINT" ;;
  "ai endpoint delete")
    [ -n "${DELETE_FAILS:-}" ] && exit 1
    touch "$CALLS.deleted"; echo '{"items": []}' > "$LISTING" ;;
  "ai endpoint create")
    echo '{"items": [{"metadata": {"name": "p2p-api", "id": "ep-new"}}]}' > "$LISTING"
    echo '{"spec": {}, "status": {"state": "RUNNING",
      "public_endpoints": ["p2p-api.endpoints.example"]}}' > "$ENDPOINT" ;;
  "ai endpoint update") [ -n "${UPDATE_FAILS:-}" ] && exit 1 ;;
esac
exit 0
"""
CURL = """#!/bin/sh
echo "curl $*" >> "$CALLS"
cat > /dev/null
case "$*" in *"/readyz"*) printf '%s' "$READYZ" ;; esac
exit 0
"""


def _run(
    tmp_path: Path,
    *args: str,
    existing: bool = False,
    url: str | None = "p2p-api.endpoints.example",
    env_lines: str = ENV_LINES,
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("nebius", NEBIUS), ("curl", CURL), ("sleep", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    listing = tmp_path / "listing.json"
    items = [{"metadata": {"name": "p2p-api", "id": "ep-123"}}] if existing else []
    listing.write_text(json.dumps({"items": items}))
    endpoint = tmp_path / "endpoint.json"
    status: dict[str, object] = {"state": "STOPPED", "public_endpoints": [url] if url else []}
    endpoint.write_text(json.dumps({"spec": {"image": OLD}, "status": status}))
    env_file = tmp_path / "prod.env"
    env_file.write_text(env_lines, encoding="utf-8", newline="\n")
    calls = tmp_path / "calls.log"
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "LISTING": str(listing),
        "ENDPOINT": str(endpoint),
        "READYZ": '{"checks":{"db":true},"commit":"abc1234"}',
        "PY": shutil.which("python") or "python",
        "P2P_CONTROL_PROJECT_ID": "project-control",
        "SUBNET_CONTROL_ID": "vpcsubnet-control",
        "P2P_EXPECT_COMMIT": "abc1234",
        "P2P_READY_ATTEMPTS": "2",
        "P2P_WAIT_ATTEMPTS": "2",
    }
    assert BASH is not None
    done = subprocess.run(  # noqa: S603 - the repo's own script with a test environment
        [BASH, str(SCRIPT), *args, str(env_file)],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines


def _nebius(calls: list[str], verb: str) -> list[str]:
    return [c for c in calls if c.startswith(f"nebius ai endpoint {verb}")]


def _index(calls: list[str], prefix: str) -> int:
    return next(i for i, c in enumerate(calls) if c.startswith(prefix) or prefix in c)


def test_a_tag_is_refused(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "cr.eu-north1.nebius.cloud/reg/p2p-api:latest")
    assert done.returncode != 0 and "digest" in done.stderr
    assert calls == []


@pytest.mark.parametrize(
    "missing", ["P2P_CONTROL_PROJECT_ID", "SUBNET_CONTROL_ID", "P2P_EXPECT_COMMIT"]
)
def test_required_inputs_are_checked_before_anything_runs(tmp_path: Path, missing: str) -> None:
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, **{missing: ""})
    assert done.returncode != 0 and missing in done.stderr
    assert calls == []


def test_a_new_endpoint_is_created_by_digest_with_secrets_by_name(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE)
    assert done.returncode == 0, done.stderr
    [create] = _nebius(calls, "create")
    assert "--parent-id project-control" in create and f"--image {IMAGE}" in create
    assert "--container-port 8080" in create and "--disk-size 30Gi" in create
    pairs = ",".join(f"{key}={value}" for key, value in REQUIRED.items())
    assert f"--env P2P_ENV=prod,{pairs} " in create
    assert "P2P_DB_BOOTSTRAP" not in create
    assert "NEBIUS_API_KEY=p2p-secrets" in create and "ADMIN_TOKEN=p2p-secrets" in create
    assert "JOB_LAUNCHER_AUTH_KEY=p2p-secrets" in create
    assert "RUNTIME_AUTH_KEY=p2p-secrets" in create
    assert all("--parent-id project-control" in c for c in _nebius(calls, "list"))
    assert "ready at https://p2p-api.endpoints.example" in done.stdout


def test_the_allowed_settings_are_exactly_the_documented_ones() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r'^ENV_KEYS="([^"]*)"', script, re.MULTILINE)
    assert block is not None
    example = (ROOT / "deploy" / "prod.env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", example, re.MULTILINE))
    assert set(block.group(1).split()) == documented


def test_every_documented_setting_is_accepted(tmp_path: Path) -> None:
    example = (ROOT / "deploy" / "prod.env.example").read_text(encoding="utf-8")
    settings = [  # the example's job images are placeholders, not digests
        f"{key}={REQUIRED[key]}" if key.endswith("_IMAGE") else line
        for line in example.splitlines()
        if line and not line.startswith("#")
        for key in [line.split("=", 1)[0]]
    ]
    settings += [  # the optional ones the example leaves commented out, with valid values
        "P2P_FORWARDED_ALLOW_IPS=10.0.0.0/8",
        "P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443",
        "OBSERVABILITY_PROJECT_ID=project-abc123",
        "P2P_METRICS_ENDPOINT=https://collector.example:4317",
    ]
    done, calls = _run(tmp_path, IMAGE, env_lines="\n".join(settings) + "\n")
    assert done.returncode == 0, done.stderr
    [create] = _nebius(calls, "create")
    assert f"--env P2P_ENV=prod,{','.join(settings)} " in create


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_a_missing_required_setting_stops_before_anything_runs(
    tmp_path: Path, missing: str
) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in REQUIRED.items() if k != missing)
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, env_lines=lines)
    assert done.returncode != 0 and missing in done.stderr
    assert calls == []


@pytest.mark.parametrize("empty", sorted(REQUIRED))
def test_an_empty_required_setting_counts_as_missing(tmp_path: Path, empty: str) -> None:
    lines = "".join(f"{k}={'' if k == empty else v}\n" for k, v in REQUIRED.items())
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, env_lines=lines)
    assert done.returncode != 0 and empty in done.stderr
    assert calls == []


@pytest.mark.parametrize(
    "key", ["P2P_PROVISIONER_IMAGE", "P2P_RUNNER_IMAGE", "P2P_RUNNER_CPU_IMAGE"]
)
def test_a_job_image_must_be_a_digest(tmp_path: Path, key: str) -> None:
    tag = f"{key}=cr.eu-north1.nebius.cloud/reg/p2p-job:latest"
    lines = "".join(tag + "\n" if k == key else f"{k}={v}\n" for k, v in REQUIRED.items())
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, env_lines=lines)
    assert done.returncode != 0 and key in done.stderr
    assert calls == []


def test_a_missing_python_is_named_as_such(tmp_path: Path) -> None:
    lines = ENV_LINES + "P2P_FORWARDED_ALLOW_IPS=10.0.0.0/8\n"  # checked with Python
    done, calls = _run(tmp_path, IMAGE, env_lines=lines, PY="no-such-python-p2p")
    assert done.returncode != 0 and "no-such-python-p2p" in done.stderr
    assert "P2P_FORWARDED_ALLOW_IPS" not in done.stderr
    assert calls == []


@pytest.mark.parametrize(
    "value",
    ["*", "0.0.0.0/0", "0.0.0.0/00", "::/0", "0.0.0.0/1;128.0.0.0/1", "10.0.0.0/7",
     "proxy.example", "10.0.0.0/8;", "999.0.0.1", "10.0.0.1/8"],
)  # fmt: skip
def test_a_wide_or_odd_proxy_list_stops_before_anything_runs(tmp_path: Path, value: str) -> None:
    lines = ENV_LINES + f"P2P_FORWARDED_ALLOW_IPS={value}\n"
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, env_lines=lines)
    assert done.returncode != 0 and "P2P_FORWARDED_ALLOW_IPS" in done.stderr
    assert calls == []


@pytest.mark.parametrize("value", ["yes", "True", ""])
def test_the_gpu_mode_is_true_or_false(tmp_path: Path, value: str) -> None:
    lines = ENV_LINES.replace("P2P_GPU_PREEMPTIBLE=true", f"P2P_GPU_PREEMPTIBLE={value}")
    done, calls = _run(tmp_path, IMAGE, env_lines=lines)
    assert done.returncode != 0 and "P2P_GPU_PREEMPTIBLE" in done.stderr
    assert calls == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [("P2P_SECRET_NAME", "p2p-secrets,EXTRA=other"), ("P2P_ENDPOINT_NAME", "p2p api")],
)
def test_odd_endpoint_or_secret_names_are_refused(
    tmp_path: Path, variable: str, value: str
) -> None:
    done, calls = _run(tmp_path, IMAGE, **{variable: value})
    assert done.returncode != 0 and variable in done.stderr
    assert calls == []


def test_bootstrap_is_only_for_the_first_create(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "--bootstrap", IMAGE)
    assert done.returncode == 0, done.stderr
    [create] = _nebius(calls, "create")
    assert ",P2P_DB_BOOTSTRAP=1 " in create + " "


def test_bootstrap_is_refused_when_the_endpoint_exists(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "--bootstrap", IMAGE, existing=True, ADMIN_TOKEN="t" * 40)
    assert done.returncode != 0 and "first deploy" in done.stderr
    assert not [c for c in calls if "drain" in c or "delete" in c or "create" in c]


def test_a_failed_listing_never_means_not_found(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE, LIST_FAILS="1")
    assert done.returncode != 0 and "list failed" in done.stderr
    assert not _nebius(calls, "create") and not _nebius(calls, "delete")


def test_recreate_drains_stops_deletes_waits_then_creates(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40)
    assert done.returncode == 0, done.stderr
    assert f"current image: {OLD}" in done.stdout
    drain = _index(calls, "/admin/drain")
    stop = _index(calls, "nebius ai endpoint stop")
    delete = _index(calls, "nebius ai endpoint delete")
    create = _index(calls, "nebius ai endpoint create")
    assert drain < stop < delete < create
    assert any(c.startswith("nebius ai endpoint list") for c in calls[delete:create])
    assert "t" * 40 not in "\n".join(calls) + done.stdout + done.stderr  # token via stdin only


def test_a_listing_error_after_the_delete_is_not_gone(tmp_path: Path) -> None:
    done, calls = _run(
        tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, LIST_FAILS_AFTER_DELETE="1"
    )
    assert done.returncode != 0
    assert _nebius(calls, "delete") and not _nebius(calls, "create")


def test_a_failed_delete_undrains_the_old_instance(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, DELETE_FAILS="1")
    assert done.returncode != 0
    drains = [c for c in calls if "/admin/drain" in c]
    assert len(drains) == 2 and '{"on": false}' in drains[1]


def test_an_in_place_update_is_opt_in_and_undrains_on_failure(tmp_path: Path) -> None:
    done, calls = _run(
        tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, P2P_ENDPOINT_UPDATE="update",
        UPDATE_FAILS="1",
    )  # fmt: skip
    assert done.returncode != 0
    assert _nebius(calls, "update") and not _nebius(calls, "delete")
    drains = [c for c in calls if "/admin/drain" in c]
    assert len(drains) == 2 and '{"on": false}' in drains[1]


def test_readiness_waits_for_the_new_build(tmp_path: Path) -> None:
    done, _ = _run(tmp_path, IMAGE, READYZ='{"checks":{"db":true},"commit":"0ld0000"}')
    assert done.returncode != 0 and "abc1234" in done.stderr


def test_a_replace_without_drain_credentials_stops(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE, existing=True)
    assert done.returncode != 0 and "drain" in done.stderr
    assert not _nebius(calls, "delete") and not _nebius(calls, "update")


def test_a_rollback_of_a_dead_instance_skips_the_drain(tmp_path: Path) -> None:
    # crash-looping: no URL and no token, yet the rollback goes through
    done, calls = _run(tmp_path, OLD, existing=True, url=None, P2P_SKIP_DRAIN="1")
    assert done.returncode == 0, done.stderr
    assert not [c for c in calls if "/admin/drain" in c]
    assert _nebius(calls, "create")


def test_a_failed_rollback_without_drain_sends_no_undrain(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, OLD, existing=True, P2P_SKIP_DRAIN="1", DELETE_FAILS="1")
    assert done.returncode != 0 and "unbound" not in done.stderr
    assert not [c for c in calls if c.startswith("curl")]


def test_a_plain_http_endpoint_is_refused(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE, existing=True, url="http://p2p.example", ADMIN_TOKEN="t")
    assert done.returncode != 0 and "https" in done.stderr
    assert not _nebius(calls, "delete")


@pytest.mark.parametrize(
    "line",
    ["P2P_ENV=dev", "DB_URL=x", "SMOKE_EXECUTOR=local", "P2P_REPLICATION=litestream",
     "P2P_DB_BOOTSTRAP=1", "P2P_BUILD_COMMIT=abc", "SESSION_KEY=x", "lowercase=x", "A=1,B=2",
     "sk-raw-secret-pasted-alone", "NEBIUS_SANDBOX_PROJECT=x", "SANDBOX_PROJECT_ID=x",
     "NEBIUS_API_KEY=x", "OBSERVABILITY_STATIC_KEY=x"],
)  # fmt: skip
def test_the_env_file_holds_only_deploy_settings(tmp_path: Path, line: str) -> None:
    done, calls = _run(tmp_path, IMAGE, env_lines=f"{line}\n")
    assert done.returncode != 0
    assert "line 1" in done.stderr and "sk-raw-secret" not in done.stderr
    assert calls == []


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t", DRY_RUN="1")
    assert done.returncode == 0, done.stderr
    assert "DRY-RUN: nebius ai endpoint delete" in done.stdout
    assert not _nebius(calls, "delete") and not _nebius(calls, "create")
    assert not [c for c in calls if c.startswith("curl")]


def test_observability_settings_are_deploy_keys_and_its_key_a_secret(tmp_path: Path) -> None:
    lines = (
        "P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443\n"
        "P2P_METRICS_ENDPOINT=https://otel-collector.example:4317\n"
        "OBSERVABILITY_PROJECT_ID=project-e00control\n"
    )
    done, calls = _run(tmp_path, IMAGE, env_lines=ENV_LINES + lines)
    assert done.returncode == 0, done.stderr
    [create] = _nebius(calls, "create")
    assert "P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443" in create
    assert "OBSERVABILITY_PROJECT_ID=project-e00control" in create
    assert "OBSERVABILITY_STATIC_KEY=p2p-secrets" in create


def test_without_tracing_the_observability_key_is_not_required(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, IMAGE)
    assert done.returncode == 0, done.stderr
    [create] = _nebius(calls, "create")
    assert "OBSERVABILITY_STATIC_KEY" not in create


@pytest.mark.parametrize(
    ("lines", "problem"),
    [
        (
            "P2P_TRACES_ENDPOINT=http://write.tracing.eu-north1.nebius.cloud:443\n"
            "OBSERVABILITY_PROJECT_ID=project-e00control\n",
            "P2P_TRACES_ENDPOINT",
        ),
        ("P2P_METRICS_ENDPOINT=collector:4317\n", "P2P_METRICS_ENDPOINT"),
        (
            "P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443\n",
            "OBSERVABILITY_PROJECT_ID",
        ),
        (
            "P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443\n"
            "OBSERVABILITY_PROJECT_ID=bad id\n",
            "OBSERVABILITY_PROJECT_ID",
        ),
    ],
)
def test_bad_observability_settings_stop_before_anything_runs(
    tmp_path: Path, lines: str, problem: str
) -> None:
    done, calls = _run(
        tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, env_lines=ENV_LINES + lines
    )
    assert done.returncode != 0 and problem in done.stderr
    assert calls == []


def test_a_key_set_twice_is_refused(tmp_path: Path) -> None:
    lines = "S3_REGION=eu-north1\nS3_REGION=us-east-1\n"
    done, calls = _run(tmp_path, IMAGE, existing=True, ADMIN_TOKEN="t" * 40, env_lines=lines)
    assert done.returncode != 0 and "S3_REGION is set twice" in done.stderr
    assert calls == []
