#!/usr/bin/env bash
# Disaster-recovery drill (A-32, H-08; runbook in docs/OPERATIONS.md): start a run, delete and
# recreate the p2p-api endpoint with the image it runs now (deploy/endpoint.sh, recreate mode),
# check that the database came back from its Litestream replica (images/api/start.sh restores
# it at start), that the in-flight run resumes, and measure the RTO.
# Human step: it deletes the production endpoint and starts a run; never run it from an agent.
#
# Usage: deploy/dr_drill.sh [--dry-run] <arxiv-id> [env-file]
#   --dry-run  read-only lookups only (nebius list/get, GET /readyz); every change (endpoint
#              stop/delete/create, drain, login, run creation, cancel) is printed, not made
#   arxiv-id   the paper of the drill's run (pick a cheap one: the run is cancelled at the end)
#   env-file   passed to deploy/endpoint.sh (default deploy/prod.env)
# Environment:
#   P2P_CONTROL_PROJECT_ID, SUBNET_CONTROL_ID, ADMIN_TOKEN   as for deploy/endpoint.sh
#   P2P_DRILL_INVITE     invite code for the drill's run (sent on stdin, never on argv)
#   P2P_EXPECT_COMMIT    optional: the commit the running image must report on /readyz (checked
#                        before any change; the drill always redeploys the commit it reports)
#   P2P_DRILL_KEEP_RUN=1 leave the drill's run going at the end (default: cancel it)
#   P2P_RUN_ATTEMPTS     polls 10 s apart for the run to start / resume (default 60, at least 1)
# P2P_DRILL_INVITE, DRY_RUN, P2P_SKIP_DRAIN and P2P_DRAIN_WAIT_S are never passed on to
# deploy/endpoint.sh.
#
# The session cookie is host-only (__Host-, no Domain), and the recreated endpoint may get a
# new hostname: after the login its value goes into a header file in a private temporary
# directory and every request sends it from there (never on argv). Only that session owns the
# run (runs are private, cancel is owner-only), so a failed drill cancels the run before it
# deletes the session, and keeps the session file when the cancel fails or when the run
# creation failed in a way that may still have created a run (a timeout, a status other than
# the app's refusals, no run id, an interrupt during the request).
set -euo pipefail

NAME="${P2P_ENDPOINT_NAME:-p2p-api}"
PY="${PY:-python3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENDPOINT_SH="${P2P_ENDPOINT_SH:-$HERE/endpoint.sh}"
SESSION_COOKIE="__Host-p2p_session"  # app/api/deps.py

die() { echo "dr_drill.sh: FAILED: $*" >&2; exit 1; }
now() { date -u +%s; }
step() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# --- every input is checked before anything changes ---------------------------------------
dry=0
if [ "${1:-}" = --dry-run ]; then dry=1; shift; fi
arxiv="${1:-}"
env_file="${2:-deploy/prod.env}"
[[ "$arxiv" =~ ^[0-9]{4}\.[0-9]{4,5}(v[0-9]+)?$ ]] \
  || die "usage: deploy/dr_drill.sh [--dry-run] <arxiv-id> [env-file] (an arXiv ID like 2410.01234)"
[ -f "$env_file" ] || die "missing $env_file (copy deploy/prod.env.example)"
[ -f "$ENDPOINT_SH" ] || die "missing $ENDPOINT_SH"
project="${P2P_CONTROL_PROJECT_ID:-}"
[[ "$project" =~ ^[a-z0-9-]+$ ]] || die "P2P_CONTROL_PROJECT_ID is required (the p2p-control project)"
[[ "${SUBNET_CONTROL_ID:-}" =~ ^[a-z0-9-]+$ ]] || die "SUBNET_CONTROL_ID is required"
[ -n "${ADMIN_TOKEN:-}" ] || die "ADMIN_TOKEN is required (deploy/endpoint.sh drains the endpoint)"
[ -n "${P2P_DRILL_INVITE:-}" ] || die "P2P_DRILL_INVITE is required (an invite code for the drill's run)"
expect="${P2P_EXPECT_COMMIT:-}"
[ -z "$expect" ] || [[ "$expect" =~ ^[0-9a-f]{7,40}$ ]] || die "P2P_EXPECT_COMMIT is not a commit: $expect"
attempts="${P2P_RUN_ATTEMPTS:-60}"
[[ "$attempts" =~ ^[1-9][0-9]*$ ]] || die "P2P_RUN_ATTEMPTS must be a number of at least 1"

work="$(mktemp -d)"  # private (0700): the session lives here
cookies="$work/cookies.txt"
cookie_hdr="$work/cookie.hdr"
drill_started="$(now)"
run_id=""
url=""
keep_session=0
creating=0  # 1 while the outcome of POST /api/runs is unknown

cancel_run() {  # POST /api/runs/<run>/cancel with the drill's session; fails without one
  [ -f "$cookie_hdr" ] || return 1
  curl -fsS --proto =https --max-time 20 -X POST -H @"$cookie_hdr" \
    "$url/api/runs/$run_id/cancel" >/dev/null
}

session_kept() {  # session_kept <why>: keep only the session file and say how to cancel
  keep_session=1
  rm -f "$cookies"
  if [ -n "$run_id" ]; then
    echo "dr_drill.sh: $1: the drill's run $run_id is still going. Only the drill's session owns" >&2
  else  # the run id never reached the drill: the admin listing and the logs have it
    echo "dr_drill.sh: $1: find its id (newest first, arXiv $arxiv) with" >&2
    echo "  uv run python -m app.admin_cli --url $url runs --limit 5" >&2
    echo "dr_drill.sh: or in the server logs, and put it in place of RUN_ID_HERE below. Only the" >&2
    echo "dr_drill.sh: drill's session owns" >&2
  fi
  echo "dr_drill.sh: it (kept in $cookie_hdr; replace the URL if the endpoint moved). Cancel with:" >&2
  echo "  curl -fsS --proto =https -X POST -H @$cookie_hdr $url/api/runs/${run_id:-RUN_ID_HERE}/cancel && rm -rf $work" >&2
}

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    echo "dr_drill.sh: stopped after $(( $(now) - drill_started )) s" >&2
    if [ "$creating" = 1 ] && [ -z "$run_id" ] && [ "$keep_session" != 1 ]; then
      session_kept "interrupted during the run creation: a run may have been created"
    elif [ -n "$run_id" ] && [ "$dry" != 1 ] && [ "$keep_session" != 1 ]; then
      if cancel_run; then
        echo "dr_drill.sh: cancelled the drill's run $run_id" >&2
      else
        session_kept "the cancel failed"
      fi
    fi
  fi
  [ "$keep_session" = 1 ] || rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT  # an interrupted drill still cancels its run
trap 'exit 143' TERM

# --- reading the platform and the app (fail closed: an error never means "fine") -----------
# JSON goes to python on stdin, never on argv.
endpoint_id() {
  nebius ai endpoint list --parent-id "$project" --format json >"$work/listing.json" \
    || die "nebius ai endpoint list failed"
  "$PY" -c '
import json, sys
data = json.load(sys.stdin)
if data.get("next_page_token"):
    sys.exit("the listing has more pages: look the endpoint up by hand")
ids = [i["metadata"]["id"] for i in data.get("items") or []
       if i.get("metadata", {}).get("name") == sys.argv[1]]
if len(ids) > 1:
    sys.exit("several endpoints share the name " + sys.argv[1])
print(ids[0] if ids else "")' "$NAME" <"$work/listing.json" || die "could not read the endpoint listing"
}

endpoint_field() {  # endpoint_field <id> url|image
  nebius ai endpoint get --id "$1" --format json >"$work/endpoint.json" \
    || die "nebius ai endpoint get failed"
  "$PY" -c '
import json, sys
data = json.load(sys.stdin)
if sys.argv[1] == "image":
    print(data.get("spec", {}).get("image", ""))
else:
    for u in data.get("status", {}).get("public_endpoints", []):
        u = u if isinstance(u, str) else (u.get("url") or u.get("address") or "")
        if u:
            print(u if u.startswith("http") else "https://" + u)
            break' "$2" <"$work/endpoint.json" || die "could not read endpoint $1"
}

https_url() {
  [[ "$1" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "the endpoint URL is not https: $1"
  printf '%s' "$1"
}

json_field() {  # json_field <python expression on `d`> <<<json
  "$PY" -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"
}

get() {  # get <path>: GET with the drill's session (read-only, also in a dry run)
  curl -fsS --proto =https --max-time 20 -H @"$cookie_hdr" "$url$1"
}

login() {  # the invite code on stdin; the session cookie from the jar into $cookie_hdr
  if [ "$dry" = 1 ]; then echo "DRY-RUN: POST $url/api/session" >&2; return; fi
  "$PY" -c 'import json, os, sys; sys.stdout.write(json.dumps({"invite_code": os.environ["P2P_DRILL_INVITE"]}))' \
    | curl -fsS --proto =https --max-time 20 -c "$cookies" -H 'Content-Type: application/json' \
      --data-binary @- "$url/api/session" >/dev/null || return 1
  (umask 077 && "$PY" -c '
import re, sys
name, value = sys.argv[1], None
for line in sys.stdin:
    line = line.rstrip("\r\n")
    if line.startswith("#HttpOnly_"):
        line = line[len("#HttpOnly_"):]
    elif not line or line.startswith("#"):
        continue
    fields = line.split("\t")
    if len(fields) == 7 and fields[5] == name:
        value = fields[6]
if not value or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", value):
    sys.exit("no session cookie " + name + " after the login")
sys.stdout.write("Cookie: " + name + "=" + value + "\n")' "$SESSION_COOKIE" <"$cookies" >"$cookie_hdr") \
    || return 1
  rm -f "$cookies"
}

create_run() {  # POST /api/runs: prints the HTTP status, the body goes to $work/created.json
  "$PY" -c 'import json, sys; sys.stdout.write(json.dumps({"arxiv_id": sys.argv[1]}))' "$arxiv" \
    | curl -sS --proto =https --max-time 20 -o "$work/created.json" -w '%{http_code}' \
      -H @"$cookie_hdr" -H 'Content-Type: application/json' --data-binary @- "$url/api/runs"
}

refused() {  # refused <status>: the app refused before creating a run (app/api/runs.py, the
  # session and middleware checks), with why; any other status may come after a creation
  case "$1" in
    400) echo "the app refused the arXiv ID" ;;
    401 | 403) echo "the app refused the drill's session" ;;
    402) echo "the kill switch is on (daily budget reached)" ;;
    413) echo "the app refused the request body" ;;
    429) echo "the invite's daily quota is used up, or too many requests" ;;
    503) echo "the endpoint is draining (a deploy is in progress): turn drain off first" ;;
    *) return 1 ;;
  esac
}

last_event() {  # last_event <run_id>: "<last id> <its type> <step of the last step.started> <attempt>"
  get "/api/runs/$1/events.json?after=0" | "$PY" -c '
import json, sys
events = json.load(sys.stdin)["events"]
started = [e for e in events if e["type"] == "step.started"]
last = events[-1] if events else {"id": 0, "type": "none"}
step = started[-1]["payload"] if started else {"step": "none", "attempt": 0}
print(int(last["id"]), last["type"], step["step"], int(step["attempt"]))'
}

# --- 1. the endpoint as it runs now ---------------------------------------------------------
step "looking up $NAME in $project"
old_id="$(endpoint_id)"
[ -n "$old_id" ] || die "$NAME does not exist in $project: nothing to drill"
image="$(endpoint_field "$old_id" image)"
[[ "$image" =~ ^[-a-z0-9./:]+@sha256:[0-9a-f]{64}$ ]] \
  || die "the running image is not a digest reference: ${image:-?}"
url="$(https_url "$(endpoint_field "$old_id" url)")"
ready="$(curl -fsS --proto =https --max-time 10 "$url/readyz")" \
  || die "$url/readyz is not ready: fix the endpoint before drilling"
commit="$(json_field 'd.get("commit", "")' <<<"$ready")" || commit=""
[[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] || die "could not read the running commit from $url/readyz"
[ -z "$expect" ] || [ "$expect" = "$commit" ] \
  || die "P2P_EXPECT_COMMIT is $expect but $url/readyz reports $commit: nothing was changed"
step "endpoint $old_id at $url runs $image (commit $commit)"

# --- 2. an in-flight run ----------------------------------------------------------------------
step "starting the drill's run on arXiv $arxiv"
login || die "login with P2P_DRILL_INVITE failed"
before_id=0 before_type=none run_step=none run_attempt=0
if [ "$dry" = 1 ]; then
  echo "DRY-RUN: POST $url/api/runs" >&2
  echo "DRY-RUN: wait until the run is RUNNING with a step.started, note its last event and step"
else
  # Only the app's refusals (a 4xx it sends before creating anything, or the drain's 503) mean
  # that no run was created. A timeout, another status or an answer without a run id may hide a
  # created run that only the drill's session can cancel; so may an interrupt (see cleanup).
  creating=1 rc=0
  code="$(create_run)" || rc=$?
  answer="HTTP ${code:-000}"
  [ "$rc" = 0 ] || answer="$answer, curl exit $rc"
  why=""
  if [ "$rc" = 0 ] && why="$(refused "$code")"; then
    if [ "$code" = 503 ]; then  # the app's errors are {"error": <detail>} (app/api/main.py)
      drain="$(json_field 'd.get("error")' 2>/dev/null <"$work/created.json")" || drain=""
      [ "$drain" = "deploy in progress" ] || why=""
    fi
  fi
  if [ -n "$why" ]; then
    creating=0
    die "the drill's run was not created ($answer: $why)"
  fi
  if [ "$rc" = 0 ] && [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
    run_id="$(json_field 'd["run_id"]' 2>/dev/null <"$work/created.json")" || run_id=""
    [[ "$run_id" =~ ^r_[0-9a-z]{26}$ ]] || run_id=""
  fi
  if [ -z "$run_id" ]; then
    echo "dr_drill.sh: FAILED: no run id from the run creation ($answer)" >&2
    session_kept "a run may have been created"
    exit 1
  fi
  creating=0
  status=""
  for _ in $(seq 1 "$attempts"); do
    status="$(get "/api/runs/$run_id" | json_field 'd["status"]')" || die "could not read run $run_id"
    noted="$(last_event "$run_id")" || die "could not read the events of run $run_id"
    read -r before_id before_type run_step run_attempt <<<"$noted"
    [[ "$before_id" =~ ^[0-9]+$ && "$run_attempt" =~ ^[0-9]+$ ]] \
      || die "unexpected events of run $run_id"  # they reach bash arithmetic below
    [ "$status" = RUNNING ] && [ "$run_attempt" -gt 0 ] && break
    [ "$status" = QUEUED ] || [ "$status" = RUNNING ] || die "the drill's run ended before the drill: $status"
    sleep 10
  done
  [ "$status" = RUNNING ] && [ "$run_attempt" -gt 0 ] \
    || die "the drill's run did not start a step within $attempts polls"
  step "run $run_id is RUNNING in $run_step (attempt $run_attempt), last event $before_id ($before_type)"
fi

# --- 3. delete and recreate the endpoint (drain, stop, delete, create, /readyz) ------------
# The moment deploy/endpoint.sh starts stopping the old instance is noted from its output: a
# step.started written after it comes from the new instance.
down="$(now)"
step "replacing $NAME: downtime starts"
watch_stop() {
  local line
  while IFS= read -r line; do
    printf '%s\n' "$line"
    case "$line" in "recreating $NAME "*) now >"$work/stopping_at" ;; esac
  done
}
if [ "$dry" = 1 ]; then
  env -u P2P_DRILL_INVITE -u P2P_SKIP_DRAIN -u P2P_DRAIN_WAIT_S DRY_RUN=1 P2P_ENDPOINT_UPDATE=recreate \
    P2P_EXPECT_COMMIT="$commit" bash "$ENDPOINT_SH" "$image" "$env_file" | watch_stop \
    || die "deploy/endpoint.sh failed"
else
  env -u P2P_DRILL_INVITE -u DRY_RUN -u P2P_SKIP_DRAIN -u P2P_DRAIN_WAIT_S P2P_ENDPOINT_UPDATE=recreate \
    P2P_EXPECT_COMMIT="$commit" bash "$ENDPOINT_SH" "$image" "$env_file" | watch_stop \
    || die "deploy/endpoint.sh failed: roll back with it"
fi
ready_at="$(now)"
rto=$((ready_at - down))
stopping_at="$(cat "$work/stopping_at" 2>/dev/null)" \
  || die "deploy/endpoint.sh did not say when it stopped the old instance"
step "/readyz answers with commit $commit after $rto s"
new_id="$(endpoint_id)"
[ "$dry" = 1 ] || [ -n "$new_id" ] || die "$NAME is not listed after the recreate"
[ "$dry" = 1 ] || [ "$new_id" != "$old_id" ] || die "$NAME kept its id $old_id: it was not recreated"
if [ -n "$new_id" ]; then url="$(https_url "$(endpoint_field "$new_id" url)")"; fi

# --- 4. restore and resume ------------------------------------------------------------------
cancelled=""
if [ "$dry" = 1 ]; then
  echo "DRY-RUN: GET $url/api/runs/<run>/events.json?after=<noted id - 1>: the noted event is back (restore)"
  echo "DRY-RUN: poll for a step.started of the noted step and attempt written after the stop (resume)"
  echo "DRY-RUN: POST $url/api/runs/<run>/cancel (unless P2P_DRILL_KEEP_RUN=1)"
  restore="not checked (dry run)" resume="not checked (dry run)"
else
  get "/api/runs/$run_id" >/dev/null || die "restore: run $run_id is gone after the recreate"
  get "/api/runs/$run_id/events.json?after=$((before_id - 1))" >"$work/page.json" \
    || die "restore: could not read the events of run $run_id"
  "$PY" -c '
import json, sys
events = json.load(sys.stdin)["events"]
sys.exit(0 if any(e["id"] == int(sys.argv[1]) and e["type"] == sys.argv[2] for e in events) else 1)' \
    "$before_id" "$before_type" <"$work/page.json" \
    || die "restore: event $before_id ($before_type) is missing after the recreate"
  restore="ok (run $run_id, event $before_id ($before_type) kept)"
  step "restore $restore"
  # Resumed = a step.started written after the stop for the step and attempt that were RUNNING
  # when the old instance stopped (the step.started right before it, in event order). "After the
  # stop", not after /readyz: the app resumes runs in its lifespan, before /readyz answers. The
  # stop time is the operator's clock and the event times the server's: only an event after the
  # noted one can be the resume, so a clock skew can fail the drill but never pass it.
  resumed_at="" found=""
  for _ in $(seq 1 "$attempts"); do
    status="$(get "/api/runs/$run_id" | json_field 'd["status"]')" || die "could not read run $run_id"
    get "/api/runs/$run_id/events.json?after=$((before_id - 1))" >"$work/page.json" \
      || die "could not read the events of run $run_id"
    found="$("$PY" -c '
import json, sys
from datetime import datetime, timezone
def epoch(ts):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
stopping_at, running = float(sys.argv[1]), (sys.argv[2], int(sys.argv[3]))
noted = int(sys.argv[4])
events = json.load(sys.stdin)["events"]
for e in events:
    if e["type"] != "step.started":
        continue
    this = (e["payload"]["step"], int(e["payload"]["attempt"]))
    if this == running and e["id"] > noted and epoch(e["ts"]) > stopping_at:
        print("resumed", e["ts"])
        sys.exit()
    running = this
late = any(epoch(e["ts"]) > stopping_at for e in events)
print("waiting" if late else "nothing-after-the-stop")' \
      "$stopping_at" "$run_step" "$run_attempt" "$before_id" <"$work/page.json")" \
      || die "could not read the events of run $run_id"
    if [[ "$found" == resumed* ]]; then
      resumed_at="$(now)"
      break
    fi
    if [ "$status" != RUNNING ] && [ "$status" != QUEUED ]; then
      [ "$status" = COMPLETED ] && [ "$found" = nothing-after-the-stop ] \
        && die "resume: run finished before the stop: resume not exercised (pick a longer run)"
      die "resume: the run ended $status after the recreate without resuming (check its error event)"
    fi
    sleep 10
  done
  [ -n "$resumed_at" ] || die "resume: no step.started of $run_step attempt $run_attempt after the stop for run $run_id within $attempts polls"
  [ "$status" = RUNNING ] || [ "$status" = COMPLETED ] || [ "$status" = QUEUED ] \
    || die "resume: the run ended $status after resuming (check its error event)"
  resume="ok ($run_step attempt $run_attempt started again at ${found#resumed }, status $status)"
  step "resume $resume"
  if [ "${P2P_DRILL_KEEP_RUN:-}" = 1 ]; then
    cancelled="kept (P2P_DRILL_KEEP_RUN=1)"
    session_kept "P2P_DRILL_KEEP_RUN=1"
  elif [ "$status" = COMPLETED ]; then
    cancelled="not needed (COMPLETED)"
  elif cancel_run; then
    cancelled="cancelled"
    step "run $run_id cancelled"
  else
    cancelled="FAILED: cancel it with the command above"
    session_kept "the cancel failed"
  fi
fi

trap - EXIT
[ "$keep_session" = 1 ] || rm -rf "$work"
if [ "$dry" = 1 ]; then
  when="dry run" rto_line="n/a (dry run)" recovery_line="n/a (dry run)"
else
  when="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  rto_line="$rto s (drain + stop + delete + create + restore, until /readyz)"
  recovery_line="$((resumed_at - down)) s (until the drill saw the run's step started again)"
fi
cat <<EOF

DR drill summary ($when)
  endpoint   $NAME: $old_id -> ${new_id:-?} ($url)
  image      $image (commit $commit)
  restore    $restore
  resume     $resume
  run        ${cancelled:-n/a (dry run)}
  RTO        $rto_line
  recovery   $recovery_line
EOF
case "$cancelled" in FAILED*) exit 1 ;; esac
