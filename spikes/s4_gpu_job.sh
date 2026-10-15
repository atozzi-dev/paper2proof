#!/usr/bin/env bash
# Spike S4: L40S quota in eu-north1 and a GPU Serverless Job that runs nvidia-smi.
#
# Usage: spikes/s4_gpu_job.sh [--help]
# Needs: nebius CLI logged in, parent-id = p2p-sandbox project (nebius config set parent-id ...).
# Optional env: SUBNET_ID (required if the project has several subnets), PLATFORM (gpu-l40s-a),
#               PRESET (1gpu-8vcpu-32gb), PREEMPTIBLE=1, KEEP=1 (don't delete the job).
# Cost: one L40S for ~3-6 minutes on demand, about $0.10-0.15.
# PASS = job reaches a successful terminal state and the logs show nvidia-smi with an L40S.
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# shellcheck source=spikes/lib.sh
source "$(dirname "$0")/lib.sh"
need_cmd nebius
need_cmd "$PY"

IMAGE="${IMAGE:-nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e}"
PLATFORM="${PLATFORM:-gpu-l40s-a}"
PRESET="${PRESET:-1gpu-8vcpu-32gb}"
NAME="p2p-spike-s4-$(date +%s)"

echo "== CLI preflight"
HELP="$(nebius ai job create --help 2>&1 || true)"
require_flags "$HELP" --image --container-command --platform --preset --disk-size --timeout --restart-policy
fact "nebius CLI: $(nebius version 2>/dev/null | head -1 || echo unknown)"

ARGS=(--image "$IMAGE" --container-command nvidia-smi --platform "$PLATFORM" --preset "$PRESET"
      --disk-size 50Gi --timeout 1h --restart-policy never)
[ -n "${SUBNET_ID:-}" ] && ARGS+=(--subnet-id "$SUBNET_ID")
if [ "${PREEMPTIBLE:-0}" = "1" ]; then ARGS+=(--preemptible --follows-spot-price); fi

echo "== create $NAME on $PLATFORM/$PRESET"
START=$(date +%s)
JOB_ID=""
# shellcheck disable=SC2016  # expanded at cleanup time on purpose (see lib.sh)
on_cleanup 'job_cleanup "$JOB_ID" "$NAME"'
JOB_ID="$(job_create "$NAME" "${ARGS[@]}")"
fact "job id: $JOB_ID"

job_wait "$JOB_ID" 1200
fact "final state: $JOB_FINAL_STATE after ${JOB_TOTAL_S}s (create -> RUNNING: ${JOB_RUNNING_AFTER_S:-not observed}s, wall $(( $(date +%s) - START ))s)"

LOGS="$(job_logs "$JOB_ID")"
echo "== logs (first 30 lines)"
head -30 <<<"$LOGS"

case "$JOB_FINAL_STATE" in
  *COMPLETED*|*SUCCEEDED*) ;;
  TIMEOUT) fail "job still not finished after 20 min (capacity? quota?)" ;;
  *) fail "job ended in $JOB_FINAL_STATE"; job_json "$JOB_ID" | head -60 || true ;;
esac
if grep -q "NVIDIA-SMI" <<<"$LOGS"; then
  fact "nvidia-smi ran; GPU: $(grep -oE 'L40S|H100|H200|A100|L4[^0]' <<<"$LOGS" | head -1 || echo '?')"
  DRIVER="$(grep -oE 'Driver Version: [0-9.]+ +CUDA Version: [0-9.]+' <<<"$LOGS" | head -1 || true)"
  [ -z "$DRIVER" ] || fact "$DRIVER"
else
  fail "no nvidia-smi output in logs"
fi

print_facts
echo "VERDICT: $REPORT_OUTCOME"
