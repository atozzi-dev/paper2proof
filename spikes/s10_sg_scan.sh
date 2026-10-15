#!/usr/bin/env bash
# Spike S10: does the network's default security group still apply to the gateway, and do our
# sg-egress-gw rules at priority < 500 win? Port scan from the exec subnet (run AFTER H-04).
#
# Usage: spikes/s10_sg_scan.sh [--help]
# Needs: nebius CLI (parent-id = p2p-sandbox), SUBNET_EXEC_ID, GATEWAY_PRIVATE_IP,
#        PROBE_IMAGE (python:3.12-alpine mirrored into the sandbox registry, by digest).
# Cost: one cpu-d3 2vcpu-8gb job for ~3 min (< $0.01).
# Expected (PASS): gateway 80/443 OPEN (intercepted), gateway 22/3128/3129/8080 not OPEN,
#                  direct internet on non-web ports (1.1.1.1:53, 8.8.8.8:853, example.com:22) not OPEN.
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# shellcheck source=spikes/lib.sh
source "$(dirname "$0")/lib.sh"
need_cmd nebius
need_cmd "$PY"
need_env SUBNET_EXEC_ID GATEWAY_PRIVATE_IP PROBE_IMAGE

GW="$GATEWAY_PRIVATE_IP"
SPEC_FILE="$(mktemp)"
cat >"$SPEC_FILE" <<EOF
{"mount": null,
 "tcp": [["$GW", 22], ["$GW", 80], ["$GW", 443], ["$GW", 3128], ["$GW", 3129], ["$GW", 8080],
         ["1.1.1.1", 53], ["8.8.8.8", 853], ["example.com", 22], ["example.com", 80]],
 "tls": [["example.com", 443]]}
EOF

NAME="p2p-spike-s10-$(date +%s)"
JOB_ID=""
# shellcheck disable=SC2016  # expanded at cleanup time on purpose (see lib.sh)
on_cleanup 'job_cleanup "$JOB_ID" "$NAME"; rm -f "$SPEC_FILE"'
echo "== create $NAME"
JOB_ID="$(job_create "$NAME" --image "$PROBE_IMAGE" --subnet-id "$SUBNET_EXEC_ID" \
  --platform cpu-d3 --preset 2vcpu-8gb --disk-size 30Gi --timeout 1h --restart-policy never \
  --container-command python --args /etc/p2p/net_probe.py \
  --inject-file "$SPIKES_DIR/net_probe.py:/etc/p2p/net_probe.py" \
  --inject-file "$SPEC_FILE:/etc/p2p/probe.json")"

job_wait "$JOB_ID" 900
fact "final state: $JOB_FINAL_STATE"
LOGS="$(job_logs "$JOB_ID")"
while read -r line; do fact "$line"; done < <(grep -E '^(TCP|TLS)' <<<"$LOGS" || true)
grep -q '^PROBE_END' <<<"$LOGS" || fail "probe did not complete (see job state)"

is_open() { grep -E "^TCP $1:$2 OPEN" <<<"$LOGS" >/dev/null; }
for port in 80 443; do is_open "$GW" "$port" || partial "gateway $port not reachable from exec"; done
for port in 22 3128 3129 8080; do
  if is_open "$GW" "$port"; then fail "gateway port $port reachable from exec (default SG wins?)"; fi
done
for target in "1.1.1.1 53" "8.8.8.8 853" "example.com 22"; do
  # shellcheck disable=SC2086  # intentional word splitting into host and port
  if is_open $target; then fail "direct egress open: $target"; fi
done
if grep -E '^TLS example.com:443 OK' <<<"$LOGS" >/dev/null; then fail "example.com:443 TLS succeeded from exec"; fi

print_facts
echo "VERDICT: $REPORT_OUTCOME"
