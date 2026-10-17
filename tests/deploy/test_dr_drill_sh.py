"""deploy/dr_drill.sh with stubbed nebius, curl and sleep (A-32): inputs checked before anything
changes, a dry run that only reads, and a full drill (start a run, recreate the endpoint through
deploy/endpoint.sh, check restore and resume, print the RTO) against the stubs. The session is
host-only, so the drill sends it as a header file; a failed drill still cancels its run."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "dr_drill.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")
IMAGE = "cr.eu-north1.nebius.cloud/reg/p2p-api@sha256:" + "a" * 64
RUN_ID = "r_01jb7z00000000000000000042"
SID = "sid-of-the-drill"  # the session cookie's value: never on argv, never printed
OLD_TS = "2000-01-01T00:00:00Z"  # written by the old instance, before the stop
NEW_TS = "2099-01-01T00:00:00.123456"  # written by the new instance (naive = UTC, as SQLite)

# nebius stub: `list` prints $LISTING, `get` prints $ENDPOINT; `stop` marks it STOPPED,
# `delete` empties the listing, `create` lists ep-new and makes it RUNNING on a new hostname.
NEBIUS = """#!/bin/sh
echo "nebius $*" >> "$CALLS"
case "$1 $2 $3" in
  "ai endpoint list") cat "$LISTING" ;;
  "ai endpoint get") cat "$ENDPOINT" ;;
  "ai endpoint stop") printf '%s' "$STOPPED" > "$ENDPOINT" ;;
  "ai endpoint delete") echo '{"items": []}' > "$LISTING" ;;
  "ai endpoint create")
    echo '{"items": [{"metadata": {"name": "p2p-api", "id": "ep-new"}}]}' > "$LISTING"
    printf '%s' "$RUNNING" > "$ENDPOINT" ;;
esac
exit 0
"""
# curl stub: answers the app's routes. The login writes a host-only cookie into the -c jar; a
# run's routes answer 404 (curl exit 22) unless the session arrives, either from a -b jar whose
# host matches the URL's (as real curl does) or from a -H @file "Cookie:" header. Once the
# endpoint has been recreated, the run is $STATUS_AFTER and events.json?after=1 is $AFTER_PAGE;
# $RESTORE_LOST makes the run 404, $CANCEL_FAILS fails the cancel.
CURL = r"""#!/bin/sh
echo "curl $*" >> "$CALLS"
cat > /dev/null
tab="$(printf '\t')"
url=""
for a in "$@"; do case "$a" in https://*) url="$a" ;; esac; done
host="${url#https://}"; host="${host%%/*}"
jar="" cookie=0 prev=""
for a in "$@"; do
  case "$prev" in
    -c) jar="$a" ;;
    -b) grep -Eqs "^(#HttpOnly_)?$host$tab.*${tab}__Host-p2p_session$tab$SID$" "$a" && cookie=1 ;;
    -H) case "$a" in
          @-) ;;
          @*) grep -qx "Cookie: __Host-p2p_session=$SID" "${a#@}" && cookie=1 ;;
        esac ;;
  esac
  prev="$a"
done
after=0
grep -q "endpoint create" "$CALLS" && after=1
case "$*" in
  *"/api/runs/r_"*)
    if [ "$cookie" != 1 ]; then
      echo "curl: (22) The requested URL returned error: 404" >&2; exit 22
    fi ;;
esac
case "$*" in
  *"/readyz"*) printf '%s' "$READYZ" ;;
  *"/api/session"*)
    printf '#HttpOnly_%s\tFALSE\t/\tTRUE\t0\t__Host-p2p_session\t%s\n' "$host" "$SID" > "$jar" ;;
  *"/events.json?after=0"*) printf '%s' "$BEFORE_PAGE" ;;
  *"/events.json?after=1"*)
    if [ "$after" = 1 ]; then printf '%s' "$AFTER_PAGE"; else printf '{"events": []}'; fi ;;
  *"/events.json"*) printf '{"events": []}' ;;
  *"/cancel"*) [ -n "${CANCEL_FAILS:-}" ] && exit 22 ;;
  *"/api/runs/r_"*)
    if [ "$after" = 1 ]; then
      [ -n "${RESTORE_LOST:-}" ] && exit 22
      printf '{"status": "%s"}' "$STATUS_AFTER"
    else
      printf '{"status": "RUNNING"}'
    fi ;;
  *"/api/runs"*) printf '{"run_id": "%s", "events_url": "x"}' "$RUN_ID" ;;
esac
exit 0
"""


def _event(event_id: int, kind: str, ts: str, **payload: object) -> dict[str, object]:
    return {"id": event_id, "run_id": RUN_ID, "ts": ts, "type": kind, "payload": payload}


def _page(*events: dict[str, object]) -> str:
    return json.dumps({"events": list(events)})


STATUS_1 = _event(1, "run.status", OLD_TS, status="RUNNING")
STARTED_2 = _event(2, "step.started", OLD_TS, step="EXECUTING", attempt=1)
RESUMED = _page(
    STARTED_2,
    _event(3, "run.status", NEW_TS, status="RUNNING"),
    _event(4, "step.started", NEW_TS, step="EXECUTING", attempt=1),
)


def _endpoint(state: str, url: str, image: str = IMAGE) -> str:
    return json.dumps(
        {"spec": {"image": image}, "status": {"state": state, "public_endpoints": [url]}}
    )


def _run(
    tmp_path: Path, *args: str, image: str = IMAGE, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sleep = '#!/bin/sh\necho "sleep $*" >> "$CALLS"\nexit 0\n'
    for name, body in (("nebius", NEBIUS), ("curl", CURL), ("sleep", sleep)):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps({"items": [{"metadata": {"name": "p2p-api", "id": "ep-123"}}]}))
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(_endpoint("RUNNING", "p2p-api.endpoints.example", image))
    env_file = tmp_path / "prod.env"
    env_file.write_text(
        "S3_REGION=eu-north1\nP2P_STATE_BUCKET=p2p-state\n", encoding="utf-8", newline="\n"
    )
    calls = tmp_path / "calls.log"
    calls.touch()
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path / "tmp"),
        "CALLS": str(calls),
        "LISTING": str(listing),
        "ENDPOINT": str(endpoint),
        "STOPPED": _endpoint("STOPPED", "p2p-api.endpoints.example"),
        "RUNNING": _endpoint("RUNNING", "p2p-api-2.endpoints.example"),
        "READYZ": '{"checks":{"db":true},"commit":"abc1234"}',
        "RUN_ID": RUN_ID,
        "SID": SID,
        "BEFORE_PAGE": _page(STATUS_1, STARTED_2),
        "AFTER_PAGE": RESUMED,
        "STATUS_AFTER": "RUNNING",
        "PY": shutil.which("python") or "python",
        "P2P_CONTROL_PROJECT_ID": "project-control",
        "SUBNET_CONTROL_ID": "vpcsubnet-control",
        "ADMIN_TOKEN": "admin-token-not-real",
        "P2P_DRILL_INVITE": "invite-code-not-real",
        "P2P_READY_ATTEMPTS": "2",
        "P2P_WAIT_ATTEMPTS": "2",
        "P2P_RUN_ATTEMPTS": "3",
        # inherited from the operator's shell: the real drill must not pass them on
        "P2P_DRAIN_WAIT_S": "0",
    }
    assert BASH is not None
    done = subprocess.run(  # noqa: S603 - the repo's own script with a test environment
        [BASH, str(SCRIPT), *args, str(env_file)],
        env={**base, **env},
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=120,
        check=False,
    )
    return done, calls.read_text().splitlines()


def _cancels(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("curl") and c.endswith(f"/api/runs/{RUN_ID}/cancel")]


def _bash_cat(path: str) -> str:
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - reading a file the drill left behind
        [BASH, "-c", 'cat "$1"', "cat", path], capture_output=True, text=True, check=True
    ).stdout


def test_a_dry_run_only_reads(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "--dry-run", "2410.01234")
    assert done.returncode == 0, done.stderr
    nebius = [c for c in calls if c.startswith("nebius")]
    assert nebius and all(" list " in c or " get " in c for c in nebius)  # no stop/delete/create
    curls = [c for c in calls if c.startswith("curl")]
    assert curls and all("/readyz" in c for c in curls)  # no login, run, drain or cancel
    out = done.stdout + done.stderr
    for change in ("/api/session", "/api/runs"):
        assert f"DRY-RUN: POST https://p2p-api.endpoints.example{change}" in out
    for command in ("endpoint stop", "endpoint delete", "endpoint create", "/admin/drain"):
        assert command in out, command  # printed by deploy/endpoint.sh's dry run
    assert IMAGE in out and "commit abc1234" in out
    assert "DR drill summary (dry run)" in out
    assert "RTO        n/a (dry run)" in out and "recovery   n/a (dry run)" in out
    assert "invite-code-not-real" not in out  # never echoed


def test_the_drill_recreates_checks_restore_and_resume_and_prints_the_rto(
    tmp_path: Path,
) -> None:
    done, calls = _run(tmp_path, "2410.01234")
    assert done.returncode == 0, done.stderr
    nebius = [c.split()[3] for c in calls if c.startswith("nebius ai endpoint")]
    assert [n for n in nebius if n in ("stop", "delete", "create")] == ["stop", "delete", "create"]
    curls = [c for c in calls if c.startswith("curl")]
    order = [
        next(i for i, c in enumerate(curls) if c.endswith(path))
        for path in ("/api/session", "/api/runs", "/admin/drain", "/cancel")
    ]
    assert order == sorted(order)  # login, run, drain (endpoint.sh), ..., cancel at the end
    assert any("p2p-api-2.endpoints.example/api/runs/" in c for c in curls)  # the new URL
    assert not any("invite-code-not-real" in c or SID in c for c in calls)  # stdin/file only
    assert SID not in done.stdout + done.stderr
    assert "sleep 60" in calls  # endpoint.sh's own drain wait, not the inherited 0
    out = done.stdout
    assert f"restore    ok (run {RUN_ID}, event 2 (step.started) kept)" in out
    assert "resume     ok (EXECUTING attempt 1 started again at 2099-01-01" in out
    assert "ep-123 -> ep-new" in out
    assert "RTO" in out and "recovery" in out
    assert f"run {RUN_ID} cancelled" in out
    assert len(_cancels(calls)) == 1
    assert list((tmp_path / "tmp").iterdir()) == []  # the session is gone


def test_the_session_reaches_the_new_hostname(tmp_path: Path) -> None:
    """The cookie is host-only (__Host-, no Domain): a jar would not follow the endpoint to its
    new hostname, so every request after the login carries it as a header file."""
    done, calls = _run(tmp_path, "2410.01234")
    assert done.returncode == 0, done.stderr
    after_login = [c for c in calls if c.startswith("curl") and "/api/runs" in c]
    assert after_login and all(re.search(r" -H @\S+cookie\.hdr ", c) for c in after_login)
    assert not any(" -b " in c for c in calls)


def test_the_real_drill_ignores_inherited_dry_run_and_skip_drain(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "2410.01234", DRY_RUN="1", P2P_SKIP_DRAIN="1")
    assert done.returncode == 0, done.stderr
    nebius = [c.split()[3] for c in calls if c.startswith("nebius ai endpoint")]
    assert [n for n in nebius if n in ("stop", "delete", "create")] == ["stop", "delete", "create"]
    assert any(c.startswith("curl") and c.endswith("/admin/drain") for c in calls)


def test_a_lost_database_fails_the_drill_and_cancels_nothing_it_cannot_see(
    tmp_path: Path,
) -> None:
    done, _ = _run(tmp_path, "2410.01234", RESTORE_LOST="1")
    assert done.returncode != 0
    assert "restore" in done.stderr and "FAILED" in done.stderr


def test_lost_events_fail_the_restore_check(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "2410.01234", AFTER_PAGE=_page())
    assert done.returncode != 0
    assert "restore: event 2 (step.started) is missing" in done.stderr
    assert len(_cancels(calls)) == 1  # the failed drill still cancels its run


def test_an_event_of_another_type_fails_the_restore_check(tmp_path: Path) -> None:
    other = _page(_event(2, "log", OLD_TS, line="x"))
    done, _ = _run(tmp_path, "2410.01234", AFTER_PAGE=other)
    assert done.returncode != 0
    assert "restore: event 2 (step.started) is missing" in done.stderr


@pytest.mark.parametrize(
    "page",
    [
        _page(STARTED_2, _event(3, "run.status", NEW_TS, status="RUNNING")),  # no step.started
        _page(STARTED_2, _event(3, "step.started", NEW_TS, step="VERDICT", attempt=1)),
        _page(STARTED_2, _event(3, "step.started", NEW_TS, step="EXECUTING", attempt=2)),
        _page(STARTED_2, _event(3, "step.started", OLD_TS, step="EXECUTING", attempt=1)),
    ],
    ids=["no-step-started", "another-step", "another-attempt", "before-the-stop"],
)
def test_no_resume_of_the_running_step_fails_the_drill_and_cancels_the_run(
    tmp_path: Path, page: str
) -> None:
    done, calls = _run(tmp_path, "2410.01234", AFTER_PAGE=page)
    assert done.returncode != 0
    assert "resume: no step.started of EXECUTING attempt 1" in done.stderr
    assert len(_cancels(calls)) == 1
    assert f"cancelled the drill's run {RUN_ID}" in done.stderr
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_run_that_finished_before_the_stop_does_not_count_as_resumed(tmp_path: Path) -> None:
    page = _page(STARTED_2, _event(3, "run.status", OLD_TS, status="COMPLETED"))
    done, _ = _run(tmp_path, "2410.01234", AFTER_PAGE=page, STATUS_AFTER="COMPLETED")
    assert done.returncode != 0
    assert "run finished before the stop: resume not exercised" in done.stderr
    assert "resume     ok" not in done.stdout


def test_a_run_that_ends_without_resuming_stops_the_poll(tmp_path: Path) -> None:
    page = _page(STARTED_2, _event(3, "run.status", NEW_TS, status="FAILED"))
    done, calls = _run(tmp_path, "2410.01234", AFTER_PAGE=page, STATUS_AFTER="FAILED")
    assert done.returncode != 0
    assert "resume: the run ended FAILED after the recreate without resuming" in done.stderr
    assert sum(1 for c in calls if "events.json?after=1" in c) == 2  # restore + 1 poll, no more


def test_a_failed_drill_whose_cancel_fails_keeps_the_session_and_says_how_to_cancel(
    tmp_path: Path,
) -> None:
    done, calls = _run(tmp_path, "2410.01234", AFTER_PAGE=_page(), CANCEL_FAILS="1")
    assert done.returncode != 0
    assert len(_cancels(calls)) == 1
    assert "cancel it in the UI" not in done.stderr
    match = re.search(r"-H @(\S+cookie\.hdr) (https://\S+/cancel)", done.stderr)
    assert match, done.stderr
    assert match.group(2) == f"https://p2p-api-2.endpoints.example/api/runs/{RUN_ID}/cancel"
    assert _bash_cat(match.group(1)).strip() == f"Cookie: __Host-p2p_session={SID}"
    assert SID not in done.stderr + done.stdout


def test_a_failed_final_cancel_is_not_reported_as_cancelled(tmp_path: Path) -> None:
    done, _ = _run(tmp_path, "2410.01234", CANCEL_FAILS="1")
    assert done.returncode != 0
    assert "resume     ok" in done.stdout
    assert f"run {RUN_ID} cancelled" not in done.stdout
    match = re.search(r"-H @(\S+cookie\.hdr) (https://\S+/cancel)", done.stderr)
    assert match, done.stderr
    assert _bash_cat(match.group(1)).strip() == f"Cookie: __Host-p2p_session={SID}"


def test_keeping_the_run_keeps_the_session_to_cancel_it_later(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "2410.01234", P2P_DRILL_KEEP_RUN="1")
    assert done.returncode == 0, done.stderr
    assert _cancels(calls) == []
    match = re.search(r"-H @(\S+cookie\.hdr) (https://\S+/cancel)", done.stdout + done.stderr)
    assert match


def test_a_different_running_commit_stops_before_any_change(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "2410.01234", P2P_EXPECT_COMMIT="def5678")
    assert done.returncode != 0
    assert "P2P_EXPECT_COMMIT" in done.stderr and "abc1234" in done.stderr
    assert [c for c in calls if c.startswith("curl")] == [
        c for c in calls if c.startswith("curl") and "/readyz" in c
    ]
    assert not any(" stop " in c or " delete " in c for c in calls)


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        (("https://arxiv.org/abs/2410.01234",), {}, "arXiv ID"),
        (("2410.01234",), {"P2P_DRILL_INVITE": ""}, "P2P_DRILL_INVITE"),
        (("2410.01234",), {"ADMIN_TOKEN": ""}, "ADMIN_TOKEN"),
        (("2410.01234",), {"P2P_CONTROL_PROJECT_ID": ""}, "P2P_CONTROL_PROJECT_ID"),
        (("2410.01234",), {"P2P_RUN_ATTEMPTS": "0"}, "P2P_RUN_ATTEMPTS"),
    ],
)
def test_inputs_are_checked_before_anything_runs(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str], message: str
) -> None:
    done, calls = _run(tmp_path, *args, **env)
    assert done.returncode != 0
    assert message in done.stderr
    assert calls == []


def test_a_tagged_image_is_refused(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, "--dry-run", "2410.01234", image="reg/p2p-api:latest")
    assert done.returncode != 0
    assert "not a digest reference" in done.stderr
    assert not [c for c in calls if c.startswith("curl")]
