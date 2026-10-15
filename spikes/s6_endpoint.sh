#!/usr/bin/env bash
# Spike S6: Serverless Endpoint lifecycle: create, managed HTTPS URL, is the container disk
# ephemeral across stop/start, can the image be updated in place by digest (and how long it takes).
# The deployed app is spikes/s13_sse_probe/server.py, so KEEP=1 leaves it up for spike S13.
#
# Usage: spikes/s6_endpoint.sh [--help]
# Needs: nebius CLI with parent-id = p2p-control project, curl. Optional: SUBNET_ID, KEEP=1,
#        IMAGE_A / IMAGE_B=<registry>/s6-probe@sha256:... two digests of the prebuilt
#        spikes/s13_sse_probe/Dockerfile image (only if endpoints have no --inject-file).
# Cost: cpu-d3 2vcpu-8gb for ~20-30 min (about $0.03), plus 30 GiB disk while it exists.
#       A forgotten endpoint costs ~$50/month: KEEP=1 prints the delete command.
# PASS = created, URL serves /boot, restart behaviour recorded, update-by-digest serves the new image.
# PARTIAL = no in-place update (fallback: delete + create, ~5 min downtime before go-live).
# Litestream restore is re-checked in H-05 with the real API image (needs A-06/A-14).
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# shellcheck source=spikes/lib.sh
source "$(dirname "$0")/lib.sh"
need_cmd nebius
need_cmd curl
need_cmd "$PY"

DEFAULT_IMAGE_A="python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
DEFAULT_IMAGE_B="python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"
NAME="${ENDPOINT_NAME:-p2p-spike-s6}"
SERVER="$SPIKES_DIR/s13_sse_probe/server.py"

echo "== CLI preflight"
EP_HELP="$(nebius ai endpoint --help 2>&1 || true)"
CREATE_HELP="$(nebius ai endpoint create --help 2>&1 || true)"
require_flags "$CREATE_HELP" --image --container-port --platform --preset --disk-size
fact "endpoint subcommands: $(grep -oE '^ +[a-z-]+' <<<"$EP_HELP" | tr -s ' \n' ' ')"

ARGS=(--container-port 8080 --platform cpu-d3 --preset 2vcpu-8gb --disk-size 30Gi)
if [ -n "${IMAGE_A:-}" ]; then
  # Prebuilt probe image (ENTRYPOINT runs server.py); the update needs a second digest of it.
  [ -n "${IMAGE_B:-}" ] || die "with IMAGE_A also set IMAGE_B (another digest of the probe image)"
  EXPECT_ALPINE=0
  ARGS+=(--image "$IMAGE_A")
elif grep -q -- --inject-file <<<"$CREATE_HELP"; then
  IMAGE_A="$DEFAULT_IMAGE_A"
  IMAGE_B="$DEFAULT_IMAGE_B"
  EXPECT_ALPINE=1
  require_flags "$CREATE_HELP" --container-command --args
  ARGS+=(--image "$IMAGE_A" --container-command python --args /opt/p2p/server.py
         --inject-file "$SERVER:/opt/p2p/server.py")
else
  die "endpoint create has no --inject-file: build spikes/s13_sse_probe/Dockerfile, push it, set IMAGE_A"
fi
[ -n "${SUBNET_ID:-}" ] && ARGS+=(--subnet-id "$SUBNET_ID")

ep_json() { nebius ai endpoint get "$1" --format json 2>/dev/null || nebius ai endpoint get --id "$1" --format json; }
ep_state() { ep_json "$1" | json_state || true; }
ep_id_by_name() {
  nebius ai endpoint list --format json 2>/dev/null | "$PY" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
items = data.get("items", data) if isinstance(data, dict) else data
for it in items or []:
    if it.get("metadata", {}).get("name") == sys.argv[1]:
        print(it["metadata"]["id"])
        break' "$1" || true
}
ep_wait() { # ep_wait <id> <anchored regex> <timeout>; prints seconds or TIMEOUT(<state>)
  local start state
  start=$(date +%s)
  while true; do
    state="$(ep_state "$1")"
    [ -n "$state" ] && [ "$(( $(date +%s) - start ))" -lt 15 ] && echo "    raw state: $state" >&2
    if grep -qE "$2" <<<"$state"; then echo "$(( $(date +%s) - start ))"; return 0; fi
    [ $(( $(date +%s) - start )) -lt "$3" ] || { echo "TIMEOUT(${state:-?})"; return 0; }
    sleep 10
  done
}
ep_url() {
  ep_json "$1" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
eps = d.get("status", {}).get("public_endpoints", [])
print("raw public_endpoints:", json.dumps(eps)[:300], file=sys.stderr)
for u in eps:
    u = u if isinstance(u, str) else (u.get("url") or u.get("address") or "")
    if u:
        print(u if u.startswith("http") else "https://" + u)
        break' || true
}
boot() { curl -fsS --max-time 20 "$1/boot" 2>/dev/null || echo '{"error": "unreachable"}'; }
boot_field() { "$PY" -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1" 2>/dev/null || true; }

EP_ID=""
SERVING=0
cleanup() {
  [ -n "$EP_ID" ] || EP_ID="$(ep_id_by_name "$NAME")"
  if [ -z "$EP_ID" ]; then
    echo "If an endpoint named $NAME exists, delete it: nebius ai endpoint list" >&2
    return 0
  fi
  if [ "${KEEP:-0}" = "1" ] && [ "$SERVING" = 1 ]; then
    echo "KEEP=1: endpoint left running for S13. Delete it right after: nebius ai endpoint delete --id $EP_ID"
    return 0
  fi
  nebius ai endpoint delete --id "$EP_ID" </dev/null >/dev/null 2>&1 \
    || echo "WARNING: endpoint NOT deleted (~\$50/month): nebius ai endpoint delete --id $EP_ID" >&2
}
on_cleanup cleanup

echo "== create $NAME"
START=$(date +%s)
OUT="$(nebius ai endpoint create --name "$NAME" "${ARGS[@]}" --format json)" || die "endpoint create failed"
EP_ID="$(json_get metadata.id <<<"$OUT")"
[ -n "$EP_ID" ] || die "no endpoint id in create output: $(head -c 300 <<<"$OUT")"
fact "endpoint id: $EP_ID"
fact "create -> RUNNING: $(ep_wait "$EP_ID" '(^|_)RUNNING$' 1200)s (wall $(( $(date +%s) - START ))s)"
URL="$(ep_url "$EP_ID")"
[ -n "$URL" ] || die "no public endpoint in status"
fact "url: $URL"
for _ in $(seq 1 12); do curl -fsS --max-time 10 "$URL/healthz" >/dev/null 2>&1 && break; sleep 10; done
B1="$(boot "$URL")"
fact "boot #1: $B1"
BOOT1="$(boot_field boot_id <<<"$B1")"
[ -n "$BOOT1" ] && SERVING=1

echo "== stop / start (is the container disk ephemeral?)"
nebius ai endpoint stop --id "$EP_ID" >/dev/null || fail "stop failed"
fact "stop -> STOPPED: $(ep_wait "$EP_ID" '(^|_)STOPPED$' 600)s"
nebius ai endpoint start --id "$EP_ID" >/dev/null || fail "start failed"
fact "start -> RUNNING: $(ep_wait "$EP_ID" '(^|_)RUNNING$' 900)s"
URL2="$(ep_url "$EP_ID")"
[ "$URL2" = "$URL" ] || fact "URL changed after restart: $URL2"
B2='{}'
for _ in $(seq 1 30); do
  B2="$(boot "$URL2")"
  [ -n "$(boot_field boot_id <<<"$B2")" ] && break
  sleep 10
done
fact "boot #2: $B2"
if grep -q '"marker_existed_at_boot": true' <<<"$B2"; then
  fact "container disk SURVIVED stop/start"
else
  fact "container disk is EPHEMERAL across stop/start (Litestream restore is mandatory)"
fi
BOOT2="$(boot_field boot_id <<<"$B2")"

echo "== update image in place by digest"
UPDATE_HELP="$(nebius ai endpoint update --help 2>&1 || true)"
if grep -q -- --image <<<"$UPDATE_HELP"; then
  T0=$(date +%s)
  if nebius ai endpoint update --id "$EP_ID" --image "$IMAGE_B" >/dev/null 2>&1 \
     || nebius ai endpoint update "$EP_ID" --image "$IMAGE_B" >/dev/null; then
    # The state may still read RUNNING and the old revision may keep answering: poll /boot until
    # a new boot_id (on Alpine for the default images) shows up. Downtime is measured by wall
    # clock from the first failed answer to the first answer from the new revision.
    NEW="" FIRST_FAIL=""
    while [ $(( $(date +%s) - T0 )) -lt 900 ]; do
      B3="$(boot "$URL2")"
      ID3="$(boot_field boot_id <<<"$B3")"
      if [ -z "$ID3" ] && [ -z "$FIRST_FAIL" ]; then FIRST_FAIL=$(date +%s); fi
      if [ -n "$ID3" ] && [ "$ID3" != "$BOOT2" ]; then
        if [ "$EXPECT_ALPINE" = 0 ] || grep -qi alpine <<<"$B3"; then NEW="$B3"; break; fi
      fi
      sleep 5
    done
    if [ -n "$NEW" ]; then
      DOWN=0
      [ -z "$FIRST_FAIL" ] || DOWN=$(( $(date +%s) - FIRST_FAIL ))
      fact "update: new image serving after $(( $(date +%s) - T0 ))s, ${DOWN}s without an answer"
      fact "boot #3: $NEW"
    else
      partial "update accepted but the new revision never served /boot within 15 min"
    fi
  else
    partial "endpoint update --image failed: fallback delete + create"
  fi
else
  partial "no 'endpoint update --image' in this CLI: fallback delete + create (~5 min downtime)"
fi
[ -n "$BOOT1" ] || fail "the probe never answered /boot"

print_facts
echo "VERDICT: $REPORT_OUTCOME"
