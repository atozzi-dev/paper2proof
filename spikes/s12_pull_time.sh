#!/usr/bin/env bash
# Spike S12: how long does a fresh GPU VM take to pull the (fat) runner image?
#
# Usage: spikes/s12_pull_time.sh [--help]
# Needs: nebius CLI (parent-id = p2p-sandbox). Optional env: IMAGE (default: public
#        pytorch 2.5.1 CUDA 12.4 runtime, ~3.3 GB compressed, as a stand-in until A-06 builds
#        p2p-runner; re-run with the real digest later), PLATFORM, PRESET, SUBNET_ID, KEEP=1.
# Cost: one L40S on demand for ~5-10 minutes, about $0.15-0.25.
# Measures create -> RUNNING (pull + start) and create -> finished. PASS if RUNNING < 240 s
# (the architecture threshold); PARTIAL if slower (shrink the image / move deps to p2p-cache).
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# shellcheck source=spikes/lib.sh
source "$(dirname "$0")/lib.sh"
need_cmd nebius
need_cmd "$PY"

IMAGE="${IMAGE:-pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime@sha256:c8268a92a69bd500f8be0e665b2630ee006dadaf7bfbc24249141b15ff622755}"
PLATFORM="${PLATFORM:-gpu-l40s-a}"
PRESET="${PRESET:-1gpu-8vcpu-32gb}"
NAME="p2p-spike-s12-$(date +%s)"
PROBE_FILE="$(mktemp)"
echo "import torch; print('TORCH', torch.__version__, torch.cuda.is_available(), flush=True)" >"$PROBE_FILE"
ARGS=(--image "$IMAGE" --platform "$PLATFORM" --preset "$PRESET" --disk-size 100Gi --timeout 1h
      --restart-policy never --container-command python --args /etc/p2p/s12.py
      --inject-file "$PROBE_FILE:/etc/p2p/s12.py")
[ -n "${SUBNET_ID:-}" ] && ARGS+=(--subnet-id "$SUBNET_ID")

echo "== create $NAME with $IMAGE"
JOB_ID=""
# shellcheck disable=SC2016  # expanded at cleanup time on purpose (see lib.sh)
on_cleanup 'job_cleanup "$JOB_ID" "$NAME"; rm -f "$PROBE_FILE"'
JOB_ID="$(job_create "$NAME" "${ARGS[@]}")"
job_wait "$JOB_ID" 1800
LOGS="$(job_logs "$JOB_ID")"

fact "image: $IMAGE"
fact "create -> RUNNING: ${JOB_RUNNING_AFTER_S:-not observed}s (10 s polling resolution)"
fact "create -> $JOB_FINAL_STATE: ${JOB_TOTAL_S}s"
TORCH_LINE="$(grep -m1 '^TORCH' <<<"$LOGS" || true)"
fact "container output: ${TORCH_LINE:-none}"
echo "== job JSON (look for timestamps/events to refine the pull time)"
job_json "$JOB_ID" | head -80 || true

if [ -z "${JOB_RUNNING_AFTER_S}" ]; then
  if [ -n "$TORCH_LINE" ]; then
    partial "RUNNING never observed (job too short for 10 s polling); use total time ${JOB_TOTAL_S}s as an upper bound"
  else
    fail "job did not run ($JOB_FINAL_STATE)"
  fi
elif [ "$JOB_RUNNING_AFTER_S" -gt 240 ]; then
  partial "pull+start ${JOB_RUNNING_AFTER_S}s > 240s: slim the runner image"
fi
grep -q 'True' <<<"$TORCH_LINE" || partial "torch.cuda.is_available() was not True"

print_facts
echo "VERDICT: $REPORT_OUTCOME"
