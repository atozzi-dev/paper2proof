#!/usr/bin/env bash
# Shared helpers for the spike shell scripts. Source it, don't execute it.
# Needs: the `nebius` CLI (profile already created, parent-id set) and a Python 3 on PATH.
# shellcheck disable=SC2034  # variables are used by the scripts that source this file

PY="${PY:-python3}"

# Git Bash/MSYS rewrites "src:/container/path" arguments into Windows paths and breaks
# --inject-file and --volume. Run the CLI spikes from Linux, macOS or WSL.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "ERROR: run this spike from Linux, macOS or WSL (see spikes/README.md), not Git Bash." >&2
    echo "VERDICT: FAIL" >&2
    exit 1 ;;
esac
SPIKES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT_FACTS=()
REPORT_OUTCOME="PASS"

fact() { REPORT_FACTS+=("$*"); echo "  - $*"; }
partial() { fact "PARTIAL: $*"; [ "$REPORT_OUTCOME" = "PASS" ] && REPORT_OUTCOME="PARTIAL"; return 0; }
fail() { fact "FAIL: $*"; REPORT_OUTCOME="FAIL"; }
die() { echo "ERROR: $*" >&2; echo "VERDICT: FAIL" >&2; exit 1; }

# Cleanup registry: runs once on normal exit, die, Ctrl-C, SIGTERM or a closed terminal.
# Register with `on_cleanup '<command>'` BEFORE creating the resource; commands are evaluated at
# cleanup time, so they see the latest values of variables such as JOB_ID.
CLEANUP_CMDS=()
CLEANUP_DONE=0
on_cleanup() { CLEANUP_CMDS+=("$1"); }
run_cleanup() {
  [ "$CLEANUP_DONE" = 1 ] && return 0
  CLEANUP_DONE=1
  local cmd
  for cmd in ${CLEANUP_CMDS[@]+"${CLEANUP_CMDS[@]}"}; do eval "$cmd" || true; done
}
trap 'run_cleanup' EXIT
trap 'run_cleanup; exit 130' INT
trap 'run_cleanup; exit 143' TERM HUP

print_facts() {
  echo
  echo "KEY FACTS"
  local line
  for line in ${REPORT_FACTS[@]+"${REPORT_FACTS[@]}"}; do echo "  $line"; done
}

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }
need_env() {
  local v
  for v in "$@"; do [ -n "${!v:-}" ] || die "missing env var: $v (see spikes/README.md)"; done
}

# json_get <dotted.path> : read JSON on stdin, print the value at the path (empty if absent).
json_get() {
  "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for k in sys.argv[1].split("."):
    if isinstance(d, dict):
        d = d.get(k)
    elif isinstance(d, list) and k.isdigit() and int(k) < len(d):
        d = d[int(k)]
    else:
        d = None
    if d is None:
        break
print("" if d is None else (json.dumps(d) if isinstance(d, (dict, list)) else d))
' "$1"
}

# json_state : print the first string found under a key named state/phase (depth-first).
json_state() {
  "$PY" -c '
import json, sys
def walk(d):
    if isinstance(d, dict):
        for k in ("state", "phase"):
            if isinstance(d.get(k), str):
                return d[k]
        for v in d.values():
            r = walk(v)
            if r:
                return r
    elif isinstance(d, list):
        for v in d:
            r = walk(v)
            if r:
                return r
    return ""
try:
    print(walk(json.load(sys.stdin).get("status", {})) or "")
except (ValueError, AttributeError):
    print("")
'
}

# require_flags "<help text>" --flag ... : fail early if the CLI lacks a flag we rely on.
require_flags() {
  local help="$1" missing=() f
  shift
  for f in "$@"; do grep -q -- "$f" <<<"$help" || missing+=("$f"); done
  [ "${#missing[@]}" -eq 0 ] || die "CLI does not support: ${missing[*]} (record this in RESULTS.md)"
}

# job_id_by_name <name> : look the job up in `nebius ai job list`.
job_id_by_name() {
  nebius ai job list --format json 2>/dev/null | "$PY" -c '
import json, sys
name = sys.argv[1]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
items = data.get("items", data) if isinstance(data, dict) else data
for it in items or []:
    if it.get("metadata", {}).get("name") == name:
        print(it["metadata"]["id"])
        break
' "$1"
}

# job_create <name> <create args...> : create the job, print its id.
job_create() {
  local name="$1" out id
  shift
  out="$(nebius ai job create --name "$name" "$@" --format json)" || die "job create failed for $name"
  id="$(json_get metadata.id <<<"$out")"
  [ -n "$id" ] || id="$(job_id_by_name "$name")"
  [ -n "$id" ] || die "could not find the id of job $name"
  echo "$id"
}

job_json() { nebius ai job get "$1" --format json 2>/dev/null || nebius ai job get --id "$1" --format json; }

# job_wait <id> <timeout_s> : poll every 10 s until a terminal state. Call it directly (not in $(...)):
# it sets JOB_FINAL_STATE, JOB_RUNNING_AFTER_S (first RUNNING seen) and JOB_TOTAL_S.
JOB_FINAL_STATE=""
JOB_RUNNING_AFTER_S=""
JOB_TOTAL_S=""
job_wait() {
  local id="$1" timeout="$2" start state last="" elapsed
  start=$(date +%s)
  JOB_FINAL_STATE="" JOB_RUNNING_AFTER_S=""
  while true; do
    state="$(job_json "$id" | json_state)" || state=""   # a transient API error must not abort
    elapsed=$(( $(date +%s) - start ))
    if [ "$state" != "$last" ]; then
      echo "    [${elapsed}s] state=${state:-?}"
      last="$state"
    fi
    case "$state" in
      *RUNNING*) [ -n "$JOB_RUNNING_AFTER_S" ] || JOB_RUNNING_AFTER_S=$elapsed ;;
      *COMPLETED*|*SUCCEEDED*|*FAILED*|*CANCELLED*|*ERROR*) JOB_FINAL_STATE="$state" ;;
    esac
    if [ -z "$JOB_FINAL_STATE" ] && [ "$elapsed" -ge "$timeout" ]; then JOB_FINAL_STATE="TIMEOUT"; fi
    if [ -n "$JOB_FINAL_STATE" ]; then JOB_TOTAL_S=$elapsed; return 0; fi
    sleep 10
  done
}

job_logs() { nebius ai job logs "$1" 2>&1 || true; }

# job_cleanup <id-or-empty> <name> : delete the job; if the id was never parsed, find it by name.
job_cleanup() {
  local id="${1:-}" name="${2:-}"
  if [ -z "$id" ] && [ -n "$name" ]; then id="$(job_id_by_name "$name" || true)"; fi
  [ -n "$id" ] || return 0
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: leaving job $id (delete: nebius ai job delete $id)"; return 0; fi
  nebius ai job delete "$id" >/dev/null 2>&1 && return 0
  nebius ai job cancel "$id" >/dev/null 2>&1 || true
  nebius ai job delete "$id" >/dev/null 2>&1 || nebius ai job delete --id "$id" >/dev/null 2>&1 \
    || echo "WARNING: job $id NOT deleted (it bills until its 1h timeout): nebius ai job delete $id" >&2
}
