#!/usr/bin/env bash
# Spike S5: a job in a subnet routed to the egress gateway: image pull, bucket mount, egress blocked;
# and what happens with the gateway stopped (must fail closed).
#
# Usage: spikes/s5_job_gateway.sh --mode baseline|gateway-on|gateway-off|isolated [--help]
#   baseline     day 1, before infra: default subnet, public image. Records that pull, mount and
#                open internet work without the gateway (the reference behaviour).
#   gateway-on   after H-04: exec subnet, gateway running. Expect pull OK, mount OK, egress blocked.
#   gateway-off  after H-04: stops the gateway, runs the job, starts the gateway again.
#   isolated     hybrid deploy, no gateway VM at all: exec subnet whose default route points
#                at an allocation nobody holds. Expect egress blocked; records pull and mount.
# Needs: nebius CLI (parent-id = p2p-sandbox), S5_BUCKET (the ID of an existing bucket).
# Optional: PLATFORM / PRESET (default cpu-d3 / 2vcpu-8gb; with the non-GPU vCPU quota at 0
#        use gpu-l40s-a / 1gpu-8vcpu-32gb), SUBNET_ID (baseline only).
#        isolated also: SUBNET_EXEC_ID (PROBE_IMAGE optional: the public image by default).
#        gateway modes also: SUBNET_EXEC_ID, GATEWAY_INSTANCE_ID, PROBE_IMAGE (python:3.12-alpine
#        mirrored into the sandbox registry, by digest), NEBIUS_STORAGE_HOST, NEBIUS_REGISTRY_HOST.
# Cost: one cpu-d3 2vcpu-8gb job for ~3 min per mode (< $0.01); on gpu-l40s-a about
#       $0.10-0.15 per mode, plus any wait for capacity.
set -euo pipefail

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }
MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
case "$MODE" in baseline|gateway-on|gateway-off|isolated) ;; *) usage; exit 2 ;; esac

# shellcheck source=spikes/lib.sh
source "$(dirname "$0")/lib.sh"
need_cmd nebius
need_cmd "$PY"
need_env S5_BUCKET

PUBLIC_IMAGE="python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
PLATFORM="${PLATFORM:-cpu-d3}"
PRESET="${PRESET:-2vcpu-8gb}"
DISK="30Gi"
case "$PLATFORM" in gpu-*) DISK="50Gi" ;; esac
ARGS=(--platform "$PLATFORM" --preset "$PRESET" --disk-size "$DISK" --timeout 1h --restart-policy never
      --container-command python --args /etc/p2p/net_probe.py
      --inject-file "$SPIKES_DIR/net_probe.py:/etc/p2p/net_probe.py"
      --volume "$S5_BUCKET:/mnt/run:rw")
TLS_TARGETS='["example.com", 443], ["pypi.org", 443]'

if [ "$MODE" = "baseline" ]; then
  IMAGE="${PROBE_IMAGE:-$PUBLIC_IMAGE}"
  [ -n "${SUBNET_ID:-}" ] && ARGS+=(--subnet-id "$SUBNET_ID")
elif [ "$MODE" = "isolated" ]; then
  need_env SUBNET_EXEC_ID
  case "$PLATFORM" in
    gpu-*) ;;
    *) die "isolated mode runs on the exec GPU platform: PLATFORM=gpu-l40s-a PRESET=1gpu-8vcpu-32gb" ;;
  esac
  IMAGE="${PROBE_IMAGE:-$PUBLIC_IMAGE}"
  ARGS+=(--subnet-id "$SUBNET_EXEC_ID")
  if [ -n "${NEBIUS_STORAGE_HOST:-}" ]; then
    TLS_TARGETS="$TLS_TARGETS, [\"$NEBIUS_STORAGE_HOST\", 443]"
  fi
else
  need_env SUBNET_EXEC_ID GATEWAY_INSTANCE_ID PROBE_IMAGE NEBIUS_STORAGE_HOST NEBIUS_REGISTRY_HOST
  IMAGE="$PROBE_IMAGE"
  ARGS+=(--subnet-id "$SUBNET_EXEC_ID")
  TLS_TARGETS="$TLS_TARGETS, [\"$NEBIUS_STORAGE_HOST\", 443], [\"$NEBIUS_REGISTRY_HOST\", 443]"
fi

SPEC_FILE="$(mktemp)"
cat >"$SPEC_FILE" <<EOF
{"mount": "/mnt/run", "tcp": [["1.1.1.1", 443], ["8.8.8.8", 53], ["2606:4700:4700::1111", 443]],
 "tls": [$TLS_TARGETS], "dns": ["example.com"], "meta": true}
EOF
ARGS+=(--inject-file "$SPEC_FILE:/etc/p2p/probe.json")

echo "== CLI preflight"
HELP="$(nebius ai job create --help 2>&1 || true)"
require_flags "$HELP" --inject-file --volume --subnet-id --args
# what the CLI offers to control a job's network (public IP, subnet...): decides how the hybrid
# deploy isolates exec jobs when there is no gateway
NET_FLAGS="$( (grep -oE -- '--([a-z0-9-]*-)?(public|network|subnet|ip|egress)(-[a-z0-9-]*)?' <<<"$HELP" || true) | sort -u | tr '\n' ' ')"
fact "network flags: ${NET_FLAGS:-none}"
fact "platform: $PLATFORM/$PRESET"

instance_state() { nebius compute instance get --id "$1" --format json 2>/dev/null | json_state || true; }
restore_gateway() {
  [ "$MODE" = "gateway-off" ] || return 0
  echo "== starting the gateway again"
  nebius compute instance start --id "$GATEWAY_INSTANCE_ID" >/dev/null 2>&1 || true
  local i state=""
  for i in $(seq 1 60); do
    state="$(instance_state "$GATEWAY_INSTANCE_ID")"
    case "$state" in *RUNNING*) echo "    gateway RUNNING after $((i * 10))s"; return 0 ;; esac
    sleep 10
  done
  echo "WARNING: gateway state '${state:-?}' after 10 min: nebius compute instance start --id $GATEWAY_INSTANCE_ID" >&2
}

NAME="p2p-spike-s5-$MODE-$(date +%s)"
JOB_ID=""
# Registered before the gateway is stopped: Ctrl-C or a closed terminal still restarts it.
# shellcheck disable=SC2016  # expanded at cleanup time on purpose (see lib.sh)
on_cleanup 'job_cleanup "$JOB_ID" "$NAME"; restore_gateway; rm -f "$SPEC_FILE"'
if [ "$MODE" = "gateway-off" ]; then
  echo "== stopping gateway $GATEWAY_INSTANCE_ID"
  nebius compute instance stop --id "$GATEWAY_INSTANCE_ID" >/dev/null || die "could not stop the gateway"
fi

echo "== create $NAME (image $IMAGE)"
JOB_ID="$(job_create "$NAME" --image "$IMAGE" "${ARGS[@]}")"
fact "mode=$MODE job=$JOB_ID"

job_wait "$JOB_ID" 900
fact "final state: $JOB_FINAL_STATE (RUNNING after ${JOB_RUNNING_AFTER_S:-never}s, total ${JOB_TOTAL_S}s)"
LOGS="$(job_logs "$JOB_ID")"
echo "== probe output"
grep -E '^(PROBE|MOUNT|TCP|TLS|DNS|META)' <<<"$LOGS" || echo "(no probe output)"
while read -r line; do fact "$line"; done < <(grep -E '^(MOUNT|TCP|TLS|DNS|META)' <<<"$LOGS" || true)

PULLED=0; grep -q '^PROBE_START' <<<"$LOGS" && PULLED=1
fact "image pulled and started: $([ "$PULLED" = 1 ] && echo yes || echo no)"
opened() { grep -E "^(TLS|TCP) $1:$2 (OK|OPEN)" <<<"$LOGS" >/dev/null; }
# every target printed a line, and every one is a known "could not get out" result: a timeout,
# no route, no network, no IPv6, a name that does not resolve. A missing line is a FAIL
# (truncated logs, a dead probe); anything else (REFUSED, a reset, a TLS error) means a host
# answered, so it is PARTIAL at best.
blocked_result() {
  case "$1" in
    *" TIMEOUT"|*"ERROR(gaierror"*|*"Errno 101]"*|*"Errno 113]"*|*"Errno 97]"*) return 0 ;;
    *"Network is unreachable"*|*"No route to host"*|*"Address family not supported"*) return 0 ;;
  esac
  return 1
}
every_target_blocked() {
  local target line ok=0
  for target in 1.1.1.1:443 8.8.8.8:53 2606:4700:4700::1111:443 example.com:443 pypi.org:443; do
    line="$(grep -E "^(TCP|TLS) $target " <<<"$LOGS" | head -1 || true)"
    case "$line" in
      "") fail "no probe result for $target (incomplete output)"; ok=1 ;;
      *" OK("*|*" OPEN") fail "egress NOT blocked: $line"; ok=1 ;;
      *) blocked_result "$line" || { partial "a host answered for $target: $line"; ok=1; } ;;
    esac
  done
  return "$ok"
}

case "$MODE" in
  baseline)
    [ "$PULLED" = 1 ] || fail "job never started (see state above)"
    grep -q '^MOUNT_OK' <<<"$LOGS" || fail "bucket mount by ID failed"
    if opened example.com 443 || opened 1.1.1.1 443 || opened 8.8.8.8 53; then
      fact "baseline: open internet reachable from this subnet (no isolation without a gateway)"
    else
      fact "baseline: no internet from this subnet (isolated without a gateway)"
    fi ;;
  gateway-on)
    [ "$PULLED" = 1 ] || fail "image pull through the gateway failed -> registry must be allowlisted or routed"
    grep -q '^MOUNT_OK' <<<"$LOGS" || fail "bucket mount failed with the gateway route"
    if opened example.com 443 || opened pypi.org 443 || opened 1.1.1.1 443 || opened 8.8.8.8 53; then
      fail "egress NOT blocked from exec subnet"
    fi
    opened "$NEBIUS_STORAGE_HOST" 443 || partial "storage host not reachable over TLS from exec" ;;
  gateway-off)
    fact "gateway off: pulled=$PULLED mount=$(grep -q '^MOUNT_OK' <<<"$LOGS" && echo ok || echo failed)"
    if opened example.com 443 || opened 1.1.1.1 443; then fail "gateway off but internet reachable: NOT fail-closed"; fi
    [ "$PULLED" = 1 ] || partial "with the gateway off the image cannot be pulled (jobs fail; expected fail-closed)" ;;
  isolated)
    [ "$PULLED" = 1 ] || fail "no gateway: the image could not be pulled in the exec subnet ($IMAGE)"
    grep -q '^PROBE_END' <<<"$LOGS" || fail "the probe output is incomplete (no PROBE_END)"
    grep -q '^MOUNT_OK' <<<"$LOGS" || fail "no gateway: the run bucket could not be mounted"
    every_target_blocked || true
    META="$(grep -E '^META ' <<<"$LOGS" | head -1 || true)"
    case "$META" in
      "") fail "no metadata-service result (incomplete output)" ;;
      *"token-path=200"*|*"credentials-path=200"*) fail "the metadata service hands out credentials: $META" ;;
      *"root=2"*|*"root=3"*|*"root=4"*|*"root=5"*) partial "the metadata service answers the job: $META" ;;
    esac
    # the VPC resolver answers public names by design (architecture §8.3): a fact, not a gate
    if grep -qE '^DNS example.com RESOLVED' <<<"$LOGS"; then
      fact "public names resolve from the exec subnet (known: DNS is a possible tunnel)"
    fi
    case "$IMAGE" in
      cr.*@sha256:*) ;;
      *) partial "pulled $IMAGE, not from the Nebius registry: the prod pull path is untested" ;;
    esac
    # a public IP would win over the exec route (provider docs): there must be none
    PUBLIC="$(job_json "$JOB_ID" | "$PY" -c '
import json, sys
def walk(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from walk(child, path)
    elif "public" in path.lower() and value not in (None, "", False, [], {}):
        yield f"{path}={value}"
print(" ".join(walk(json.load(sys.stdin))))
' 2>/dev/null || echo "unreadable")"
    case "$PUBLIC" in
      "") ;;
      unreadable) partial "could not read the job's network from nebius ai job get" ;;
      *) fail "the job has a public IP setting: $PUBLIC" ;;
    esac
    if [ "$REPORT_OUTCOME" = "PASS" ]; then
      fact "isolated: pull and mount work, egress blocked: exec jobs need no gateway"
    fi ;;
esac

print_facts
echo "VERDICT: $REPORT_OUTCOME"
