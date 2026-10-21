"""deploy/secrets.sh with stubbed terraform and nebius (H-04): every value the endpoint maps from
p2p-secrets lands in one new primary version, S3 secrets come from their SecretStash references,
authorized keys are generated and their files removed, and no value ever reaches a command line,
stdout or stderr (except a generated ADMIN_TOKEN, shown once)."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "secrets.sh"
BASH = shutil.which("bash")
PY = shutil.which("python") or shutil.which("python3") or "python"
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

SECRETS_ID = "mbsec-app0001"
S3_KEYS = {
    "api_runtime": {"key_id": "RUNTIMEKEYID01", "secret_reference_id": "mbsec-ref-runtime"},
    "job_launcher": {"key_id": "LAUNCHERKEYID01", "secret_reference_id": "mbsec-ref-launcher"},
}
SA_IDS = {
    "ci_deployer": "serviceaccount-ci",
    "api_runtime": "serviceaccount-runtime",
    "job_launcher": "serviceaccount-launcher",
}
API_KEY = "tf-api-key-value-0123456789"
TAVILY = "fake-tavily-value-0123456789"
PASTED_ADMIN = "a" * 40
EXPECTED = {
    "NEBIUS_API_KEY",
    "TAVILY_API_KEY",
    "SESSION_KEY",
    "ADMIN_TOKEN",
    "RUNTIME_S3_KEY_ID",
    "RUNTIME_S3_SECRET",
    "RUNTIME_AUTH_KEY",
    "JOB_LAUNCHER_AUTH_KEY",
    "JOB_LAUNCHER_S3_KEY_ID",
    "JOB_LAUNCHER_S3_SECRET",
}

TERRAFORM = """#!/bin/sh
echo "terraform $*" >> "$CALLS"
case "$*" in
  *"output -raw secrets_id"*) printf '%s' "$SECRETS_ID" ;;
  *"output -json s3_access_keys"*) cat "$S3_KEYS_FILE" ;;
  *"output -json service_account_ids"*) cat "$SA_IDS_FILE" ;;
  *"output -json env"*) printf '%s' "$ENV_JSON" ;;
  *) exit 1 ;;
esac
"""

# nebius stub: payload get answers for the two S3 references and for the app secret (its last
# written version, or $CURRENT before any write); secret-version create stores the request it
# reads from --file; auth-public-key generate writes a credentials file and records its path.
NEBIUS = """#!/bin/bash
echo "nebius $*" >> "$CALLS"
args=("$@")
arg() { local i; for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[i]}" = "$1" ] && { echo "${args[i+1]}"; return; }; done; }
case "$1 $2 $3" in
  "mysterybox payload get")
    case "$(arg --secret-id)" in
      mbsec-ref-runtime) printf '%s' "$RUNTIME_REF_PAYLOAD" ;;
      mbsec-ref-launcher) printf '%s' "$LAUNCHER_REF_PAYLOAD" ;;
      "$SECRETS_ID")
        if [ -f "$WRITTEN" ]; then
          "$PY" -c "$READ_BACK" "$WRITTEN" "${VERIFY_KEEP:-99}" "${VERIFY_TAMPER:-}"
        elif [ -f "$CURRENT" ]; then cat "$CURRENT"
        elif [ -n "${APP_GET_ERROR:-}" ]; then echo "$APP_GET_ERROR" >&2; exit 1
        else echo "rpc error: code = NotFound desc = no primary version" >&2; exit 1; fi ;;
      *) exit 1 ;;
    esac ;;
  "mysterybox secret-version create")
    [ -n "${CREATE_FAILS:-}" ] && exit 1
    cat "$(arg --file)" > "$WRITTEN" ;;
  "iam auth-public-key generate")
    sa="$(arg --service-account-id)"
    [ "$sa" = "${GENERATE_FAILS_FOR:-}" ] && exit 1
    out="$(arg --output)"; echo "$out" >> "$KEYFILES"
    "$PY" -c "$KEY_JSON" "$sa" "${GENERATE_SUB:-$sa}" > "$out" ;;
esac
exit 0
"""
# Helpers the stubs run with $PY: the read-back of the last written version (optionally cut or
# tampered with), and a pretty-printed credentials file like the CLI writes.
READ_BACK = """import json, sys
request = json.load(open(sys.argv[1]))
payload = request["spec"]["payload"][: int(sys.argv[2])]
if sys.argv[3]:
    payload[0]["string_value"] += "-changed"
print(json.dumps({"data": payload}))
"""
KEY_JSON = """import json, sys
sa, sub = sys.argv[1], sys.argv[2]
creds = {"alg": "RS256", "private-key": "PRIVATE-" + sa, "kid": "publickey-" + sa}
creds.update(iss=sub, sub=sub)
print(json.dumps({"subject-credentials": creds}, indent=2))
"""
ENV_JSON = json.dumps(
    {"NEBIUS_CONTROL_PROJECT_ID": "project-control", "NEBIUS_SANDBOX_PROJECT_ID": "project-sandbox"}
)


def _run(
    tmp_path: Path,
    *args: str,
    answers: list[str] | None = None,
    current: dict[str, str] | None = None,
    s3_keys: object = S3_KEYS,
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], list[str], dict[str, str] | None]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("terraform", TERRAFORM), ("nebius", NEBIUS)):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "s3.json").write_text(json.dumps(s3_keys), encoding="utf-8")
    (tmp_path / "sa.json").write_text(json.dumps(SA_IDS), encoding="utf-8")
    if current is not None:
        data = [{"key": k, "string_value": v} for k, v in current.items()]
        (tmp_path / "current.json").write_text(json.dumps({"data": data}), encoding="utf-8")
    calls = tmp_path / "calls.log"
    written = tmp_path / "written.json"
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "WRITTEN": str(written),
        "CURRENT": str(tmp_path / "current.json"),
        "KEYFILES": str(tmp_path / "keyfiles.log"),
        "SECRETS_ID": SECRETS_ID,
        "S3_KEYS_FILE": str(tmp_path / "s3.json"),
        "SA_IDS_FILE": str(tmp_path / "sa.json"),
        "RUNTIME_REF_PAYLOAD": '{"data":[{"key":"secret","string_value":"RUNTIME-S3-SECRET"}]}',
        "LAUNCHER_REF_PAYLOAD": '{"data":[{"key":"secret","string_value":"LAUNCHER-S3-SECRET"}]}',
        "READ_BACK": READ_BACK,
        "KEY_JSON": KEY_JSON,
        "ENV_JSON": ENV_JSON,
        "PY": PY,
        "TMPDIR": str(tmp_path),
        "P2P_TTY": str(tmp_path / "tty.txt"),  # the "terminal" a generated ADMIN_TOKEN goes to
        "P2P_TTY_IS_FILE": "1",
        "P2P_KEY_EXPIRES_AT": "2099-01-31T00:00:00Z",
        "P2P_ENV_FILE": str(tmp_path / "prod.env"),
    }
    if answers is None:  # API key, Tavily key, ADMIN_TOKEN (paste), observability (skip), confirm
        answers = [API_KEY, TAVILY, PASTED_ADMIN, "", "load"]
    assert BASH is not None
    done = subprocess.run(  # noqa: S603 - the repo's own script with a test environment
        [BASH, str(SCRIPT), *args],
        env={**base, **env},
        input="".join(f"{a}\n" for a in answers),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    payload = None
    if written.exists():
        request = json.loads(written.read_text(encoding="utf-8"))
        assert request["metadata"]["parent_id"] == SECRETS_ID
        assert request["spec"]["set_primary"] is True
        payload = {e["key"]: e["string_value"] for e in request["spec"]["payload"]}
    return done, lines, payload


def test_the_keys_are_the_ones_the_endpoint_maps() -> None:
    endpoint = (ROOT / "deploy" / "endpoint.sh").read_text(encoding="utf-8")
    block = re.search(r"^SECRET_VARS=\(([^)]*)\)", endpoint, re.MULTILINE)
    assert block is not None
    assert set(block.group(1).split()) == EXPECTED


def test_a_first_load_writes_every_key_in_one_primary_version(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert payload is not None and set(payload) == EXPECTED
    assert payload["NEBIUS_API_KEY"] == API_KEY and payload["TAVILY_API_KEY"] == TAVILY
    assert payload["ADMIN_TOKEN"] == PASTED_ADMIN
    assert payload["RUNTIME_S3_KEY_ID"] == "RUNTIMEKEYID01"
    assert payload["RUNTIME_S3_SECRET"] == "RUNTIME-S3-SECRET"
    assert payload["JOB_LAUNCHER_S3_KEY_ID"] == "LAUNCHERKEYID01"
    assert payload["JOB_LAUNCHER_S3_SECRET"] == "LAUNCHER-S3-SECRET"
    assert "PRIVATE-serviceaccount-runtime" in payload["RUNTIME_AUTH_KEY"]
    assert "PRIVATE-serviceaccount-launcher" in payload["JOB_LAUNCHER_AUTH_KEY"]
    assert len(payload["SESSION_KEY"]) >= 32 and payload["SESSION_KEY"] != payload["ADMIN_TOKEN"]
    assert len([c for c in calls if "secret-version create" in c]) == 1


def test_no_value_reaches_a_command_line_or_the_output(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert payload is not None
    seen = "\n".join(calls) + done.stdout + done.stderr
    for value in payload.values():
        if value not in ("RUNTIMEKEYID01", "LAUNCHERKEYID01"):  # key IDs are not secret
            assert value not in seen
    assert "PRIVATE-" not in seen


def test_the_authorized_key_files_are_removed(tmp_path: Path) -> None:
    done, calls, _ = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    files = (tmp_path / "keyfiles.log").read_text(encoding="utf-8").split()
    assert len(files) == 2 and not any(Path(f).exists() for f in files)
    generates = [c for c in calls if "auth-public-key generate" in c]
    assert any("--service-account-id serviceaccount-runtime" in c for c in generates)
    assert any("--service-account-id serviceaccount-launcher" in c for c in generates)
    assert all("--expires-at 2099-01-31T00:00:00Z" in c for c in generates)
    runtime = next(c for c in generates if "serviceaccount-runtime" in c)
    launcher = next(c for c in generates if "serviceaccount-launcher" in c)
    assert "--parent-id project-control" in runtime and "--parent-id project-sandbox" in launcher
    assert "kid publickey-serviceaccount-runtime" in done.stdout


def test_a_generated_admin_token_is_shown_once(tmp_path: Path) -> None:
    answers = [API_KEY, TAVILY, "", "", "load", ""]  # ADMIN_TOKEN empty: generate; Enter: saved
    done, _, payload = _run(tmp_path, answers=answers)
    assert done.returncode == 0, done.stderr
    assert payload is not None
    token = payload["ADMIN_TOKEN"]
    assert len(token) >= 32 and token != payload["SESSION_KEY"]
    shown = (tmp_path / "tty.txt").read_text(encoding="utf-8")
    assert shown.count(token) == 1  # on the terminal only, never on stdout or stderr
    assert token not in done.stdout + done.stderr


def test_without_a_terminal_an_admin_token_is_not_generated(tmp_path: Path) -> None:
    answers = [API_KEY, TAVILY, "", "", "load", ""]
    done, calls, payload = _run(tmp_path, answers=answers, P2P_TTY=str(tmp_path / "no" / "tty"))
    assert done.returncode != 0 and payload is None
    assert "password manager" in done.stderr
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_an_optional_observability_key_is_loaded_when_given(tmp_path: Path) -> None:
    answers = [API_KEY, TAVILY, PASTED_ADMIN, "obs-static-key-value", "load"]
    done, _, payload = _run(tmp_path, answers=answers)
    assert done.returncode == 0, done.stderr
    assert payload is not None and payload["OBSERVABILITY_STATIC_KEY"] == "obs-static-key-value"


@pytest.mark.parametrize(
    "answers",
    [
        ["", TAVILY, PASTED_ADMIN, "", "load"],  # no Token Factory key
        [API_KEY, "", PASTED_ADMIN, "", "load"],  # no Tavily key
        [API_KEY, TAVILY, "short", "", "load"],  # ADMIN_TOKEN under 32 characters
        [API_KEY + " x", TAVILY, PASTED_ADMIN, "", "load"],  # whitespace in a value
    ],
)
def test_bad_answers_change_nothing(tmp_path: Path, answers: list[str]) -> None:
    done, calls, payload = _run(tmp_path, answers=answers)
    assert done.returncode != 0 and payload is None
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_nothing_changes_without_the_confirmation(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, answers=[API_KEY, TAVILY, PASTED_ADMIN, "", "no"])
    assert done.returncode != 0 and payload is None
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_dry_run_reads_but_changes_nothing(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, answers=[API_KEY, TAVILY, PASTED_ADMIN, ""], DRY_RUN="1")
    assert done.returncode == 0, done.stderr
    assert payload is None
    assert not [c for c in calls if "generate" in c or "create" in c]
    assert "DRY-RUN" in done.stdout and "RUNTIME_AUTH_KEY" in done.stdout


def test_bad_terraform_outputs_stop_before_anything_runs(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, s3_keys={"api_runtime": {"key_id": "X"}})
    assert done.returncode != 0 and payload is None
    assert not [c for c in calls if c.startswith("nebius")]


def test_an_ambiguous_s3_reference_is_refused_with_key_names_only(tmp_path: Path) -> None:
    ref = '{"data":[{"key":"a","string_value":"VALUE-A"},{"key":"b","string_value":"VALUE-B"}]}'
    done, calls, payload = _run(tmp_path, RUNTIME_REF_PAYLOAD=ref)
    assert done.returncode != 0 and payload is None
    assert "a, b" in done.stderr and "VALUE-A" not in done.stderr + done.stdout
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_only_replaces_the_named_keys_and_keeps_the_rest(tmp_path: Path) -> None:
    current = {key: f"old-{key.lower()}" for key in EXPECTED}
    done, calls, payload = _run(
        tmp_path,
        "--only",
        "TAVILY_API_KEY",
        answers=["fake-tavily-new-0123", "load"],
        current=current,
    )
    assert done.returncode == 0, done.stderr
    assert payload is not None and set(payload) == EXPECTED
    assert payload["TAVILY_API_KEY"] == "fake-tavily-new-0123"
    assert all(payload[k] == current[k] for k in EXPECTED - {"TAVILY_API_KEY"})
    assert not [c for c in calls if "generate" in c]


def test_only_an_s3_half_rotates_the_whole_pair(tmp_path: Path) -> None:
    current = {key: f"old-{key.lower()}" for key in EXPECTED}
    done, _, payload = _run(
        tmp_path, "--only", "RUNTIME_S3_SECRET", answers=["load"], current=current
    )
    assert done.returncode == 0, done.stderr
    assert payload is not None
    assert payload["RUNTIME_S3_KEY_ID"] == "RUNTIMEKEYID01"
    assert payload["RUNTIME_S3_SECRET"] == "RUNTIME-S3-SECRET"
    assert payload["JOB_LAUNCHER_S3_SECRET"] == current["JOB_LAUNCHER_S3_SECRET"]
    assert not (tmp_path / "keyfiles.log").exists()  # no authorized key generated


def test_an_empty_s3_secret_is_refused(tmp_path: Path) -> None:
    ref = '{"data":[{"key":"secret","string_value":""}]}'
    done, calls, payload = _run(tmp_path, RUNTIME_REF_PAYLOAD=ref)
    assert done.returncode != 0 and payload is None
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_multi_line_values_read_back(tmp_path: Path) -> None:
    done, _, payload = _run(tmp_path)  # the stub writes pretty-printed key JSON
    assert done.returncode == 0, done.stderr
    assert payload is not None and "\n" in payload["RUNTIME_AUTH_KEY"]


def test_a_value_changed_on_read_back_fails(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, VERIFY_TAMPER="1")
    assert done.returncode != 0 and "read back" in done.stderr


def test_a_failed_second_key_writes_nothing_and_names_the_first(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, GENERATE_FAILS_FOR="serviceaccount-launcher")
    assert done.returncode != 0 and payload is None
    assert "serviceaccount-runtime" in done.stderr and "auth-public-key list" in done.stderr
    assert not [c for c in calls if "secret-version create" in c]


@pytest.mark.parametrize(
    "env",
    [
        {"CREATE_FAILS": "1"},
        {"VERIFY_KEEP": "3"},
        {"GENERATE_FAILS_FOR": "serviceaccount-launcher"},
    ],
)
def test_no_value_leaks_on_a_failure_and_the_key_folder_is_gone(
    tmp_path: Path, env: dict[str, str]
) -> None:
    done, calls, _ = _run(tmp_path, **env)
    assert done.returncode != 0
    seen = "\n".join(calls) + done.stdout + done.stderr
    for value in (API_KEY, TAVILY, PASTED_ADMIN, "RUNTIME-S3-SECRET", "PRIVATE-"):
        assert value not in seen
    for line in (tmp_path / "keyfiles.log").read_text(encoding="utf-8").split():
        assert not Path(line).parent.exists()
    stub_files = {"calls.log", "written.json", "current.json", "keyfiles.log", "s3.json", "sa.json"}
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name not in stub_files and path.parent.name != "bin":
            assert API_KEY not in path.read_text(encoding="utf-8", errors="ignore")


def test_a_failing_request_builder_writes_nothing(tmp_path: Path) -> None:
    wrapper = tmp_path / "pyfail"
    wrapper.write_text(
        f'#!/bin/sh\nfor a in "$@"; do [ "$a" = request ] && exit 1; done\nexec "{PY}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)
    done, calls, payload = _run(tmp_path, PY=str(wrapper))
    assert done.returncode != 0 and payload is None
    assert not [c for c in calls if "secret-version create" in c]


def test_only_refuses_a_current_version_with_unknown_keys(tmp_path: Path) -> None:
    current = {key: f"old-{key.lower()}" for key in EXPECTED} | {"STRAY_KEY": "x"}
    done, _calls, payload = _run(
        tmp_path,
        "--only",
        "TAVILY_API_KEY",
        answers=["fake-tavily-x-0123456789", "load"],
        current=current,
    )
    assert done.returncode != 0 and payload is None and "STRAY_KEY" in done.stderr


def test_only_needs_a_complete_current_version(tmp_path: Path) -> None:
    done, _calls, payload = _run(
        tmp_path,
        "--only",
        "TAVILY_API_KEY",
        answers=["fake-tavily-x-0123456789", "load"],
        current={},
    )
    assert done.returncode != 0 and payload is None
    assert "--replace-all" in done.stderr


def test_only_refuses_an_unknown_key(tmp_path: Path) -> None:
    done, calls, _payload = _run(tmp_path, "--only", "P2P_ENV", answers=["load"])
    assert done.returncode != 0 and "P2P_ENV" in done.stderr
    assert not [c for c in calls if c.startswith("nebius")]


def test_a_failed_write_says_how_to_clean_up_the_new_keys(tmp_path: Path) -> None:
    done, _, payload = _run(tmp_path, CREATE_FAILS="1")
    assert done.returncode != 0 and payload is None
    assert "auth-public-key list" in done.stderr


def test_a_version_that_does_not_read_back_complete_fails(tmp_path: Path) -> None:
    done, _, _payload = _run(tmp_path, VERIFY_KEEP="3")
    assert done.returncode != 0
    assert "read back" in done.stderr


def test_a_second_first_load_is_refused(tmp_path: Path) -> None:
    current = {key: f"old-{key.lower()}" for key in EXPECTED}
    done, calls, payload = _run(tmp_path, current=current)
    assert done.returncode != 0 and payload is None
    assert "--replace-all" in done.stderr
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_replace_all_writes_over_an_existing_version(tmp_path: Path) -> None:
    current = {key: f"old-{key.lower()}" for key in EXPECTED}
    done, _, payload = _run(tmp_path, "--replace-all", current=current)
    assert done.returncode == 0, done.stderr
    assert payload is not None and payload["NEBIUS_API_KEY"] == API_KEY


def test_new_authorized_keys_must_outlive_judging(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, P2P_KEY_EXPIRES_AT="2026-10-30T00:00:00Z")
    assert done.returncode != 0 and payload is None and "30 days" in done.stderr
    assert not [c for c in calls if c.startswith("nebius")]


def test_a_key_file_for_another_account_is_refused(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, GENERATE_SUB="serviceaccount-other")
    assert done.returncode != 0 and payload is None
    assert "credentials JSON" in done.stderr and "auth-public-key list" in done.stderr
    assert not [c for c in calls if "secret-version create" in c]


def test_nebius_tracing_in_prod_env_needs_the_observability_key(tmp_path: Path) -> None:
    (tmp_path / "prod.env").write_text(
        "P2P_TRACES_ENDPOINT=https://write.tracing.eu-north1.nebius.cloud:443\n", encoding="utf-8"
    )
    done, calls, payload = _run(tmp_path)
    assert done.returncode != 0 and payload is None and "OBSERVABILITY_STATIC_KEY" in done.stderr
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_an_unreadable_secret_is_not_taken_for_an_empty_one(tmp_path: Path) -> None:
    done, calls, payload = _run(tmp_path, APP_GET_ERROR="rpc error: code = PermissionDenied")
    assert done.returncode != 0 and payload is None
    assert "PermissionDenied" in done.stderr
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_after_an_ambiguous_write_the_keys_are_not_called_unused(tmp_path: Path) -> None:
    answers = [API_KEY, TAVILY, "", "", "load", ""]  # a generated ADMIN_TOKEN
    done, _, payload = _run(tmp_path, answers=answers, CREATE_FAILS="1")
    assert done.returncode != 0 and payload is None
    assert "unless" in done.stderr and "payload get" in done.stderr
    shown = (tmp_path / "tty.txt").read_text(encoding="utf-8")
    assert "ADMIN_TOKEN" in shown  # shown before the write, so it is never lost


def test_a_failed_generate_says_where_to_look(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path, GENERATE_FAILS_FOR="serviceaccount-runtime")
    assert (
        done.returncode != 0 and "auth-public-key list --parent-id project-control" in done.stderr
    )


def test_a_generated_token_never_goes_to_a_regular_file(tmp_path: Path) -> None:
    answers = [API_KEY, TAVILY, "", "", "load", ""]
    done, calls, payload = _run(tmp_path, answers=answers, P2P_TTY_IS_FILE="")
    assert done.returncode != 0 and payload is None
    assert not (tmp_path / "tty.txt").exists()
    assert not [c for c in calls if "generate" in c or "create" in c]


def test_the_private_folder_is_gone_after_a_success(tmp_path: Path) -> None:
    done, _, _ = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    folder = Path((tmp_path / "keyfiles.log").read_text(encoding="utf-8").split()[0]).parent
    assert not folder.exists()
