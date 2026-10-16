#!/usr/bin/env bash
# Deploy the control plane (A-14, architecture §11.1): create the p2p-api Serverless endpoint,
# or replace it with a new image digest after draining the running instance. Human step (H-05)
# or the CI deploy job (A-05); never run it from an agent.
#
# Usage: deploy/endpoint.sh <registry/p2p-api@sha256:...> [env-file]
#   env-file   non-secret KEY=VALUE lines for the app (default deploy/prod.env), only the keys
#              of deploy/prod.env.example; secrets come from the Nebius secret named by
#              P2P_SECRET_NAME (loaded out of band), never from here.
# Environment:
#   P2P_CONTROL_PROJECT_ID the p2p-control project (required: never the CLI's default)
#   SUBNET_CONTROL_ID      its subnet (required: checked before anything is deleted)
#   ADMIN_TOKEN            admin token for POST /admin/drain (sent on stdin, never argv)
#   P2P_ENDPOINT_UPDATE    "recreate" (default: the old instance is gone before the new one
#                          starts, so there is one database writer) or "update" once S-6
#                          shows that an in-place update stops the old instance first
#   P2P_SKIP_DRAIN=1       rollback of an unresponsive instance (nothing to drain)
#   DRY_RUN=1              print the mutating nebius commands instead of running them
# The endpoint URL is read from `nebius ai endpoint get`. Assumptions to re-check with spike
# S-6 (see the H-05 checklist in docs/DEPLOY.md): update semantics and overlap, list paging,
# the --env/--env-secret syntax, and the shape of status.public_endpoints.
set -euo pipefail

NAME="${P2P_ENDPOINT_NAME:-p2p-api}"
SECRET="${P2P_SECRET_NAME:-p2p-secrets}"
PY="${PY:-python3}"
SECRET_VARS=(NEBIUS_API_KEY TAVILY_API_KEY SESSION_KEY ADMIN_TOKEN RUNTIME_S3_KEY_ID
  RUNTIME_S3_SECRET)  # the job launcher's credentials are added with the Nebius adapters (A-20)
ENV_KEYS=" S3_ENDPOINT S3_REGION P2P_STATE_BUCKET P2P_CONTROL_BUCKET NEBIUS_SANDBOX_PROJECT_ID
  P2P_PROVISIONER_IMAGE P2P_RUNNER_IMAGE P2P_RUNNER_CPU_IMAGE P2P_SUBNET_PROVISION
  P2P_SUBNET_EXEC P2P_GPU_PREEMPTIBLE P2P_BUILD_COMMIT P2P_BUNDLE_KEY_ID
  P2P_FORWARDED_ALLOW_IPS "  # nothing else: no P2P_ENV, DB_URL, SMOKE_EXECUTOR, ... overrides

die() { echo "endpoint.sh: $*" >&2; exit 1; }
run() {
  if [ "${DRY_RUN:-}" = 1 ]; then printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

# --- every input is checked before anything changes ---------------------------------------
image="${1:-}"
env_file="${2:-deploy/prod.env}"
digest_ref='^[-a-z0-9./:]+@sha256:[0-9a-f]{64}$'
[[ "$image" =~ $digest_ref ]] \
  || die "the image must be a digest reference (registry/p2p-api@sha256:...), never a tag"
[ -f "$env_file" ] || die "missing $env_file (copy deploy/prod.env.example)"
project="${P2P_CONTROL_PROJECT_ID:-}"
[[ "$project" =~ ^[a-z0-9-]+$ ]] || die "P2P_CONTROL_PROJECT_ID is required (the p2p-control project)"
subnet="${SUBNET_CONTROL_ID:-}"
[[ "$subnet" =~ ^[a-z0-9-]+$ ]] || die "SUBNET_CONTROL_ID is required (the p2p-control subnet)"

env_pairs=()
number=0
while IFS= read -r line || [ -n "$line" ]; do
  number=$((number + 1))
  line="${line%$'\r'}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^,]*)$ ]] || die "$env_file line $number: not KEY=VALUE"
  key="${BASH_REMATCH[1]}"
  [[ "$ENV_KEYS" == *" $key "* ]] \
    || die "$env_file line $number: $key is not a deploy setting (see deploy/prod.env.example)"
  env_pairs+=("$line")
done <"$env_file"
env_arg="$(IFS=,; echo "P2P_ENV=prod,${env_pairs[*]}")"
secret_arg="$(printf '%s,' "${SECRET_VARS[@]/%/=$SECRET}")"
secret_arg="${secret_arg%,}"
commit="$(printf '%s\n' "${env_pairs[@]}" | sed -n 's/^P2P_BUILD_COMMIT=//p')"

# --- reading the platform (fail closed: an error never means "not found") ----------------
endpoint_id() {
  local listing
  listing="$(nebius ai endpoint list --parent-id "$project" --format json)" \
    || die "nebius ai endpoint list failed"
  "$PY" -c '
import json, sys
data = json.loads(sys.argv[2])
if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
    sys.exit("unexpected listing")
if data.get("next_page_token"):
    sys.exit("the listing has more pages: look the endpoint up by hand")
ids = [i["metadata"]["id"] for i in data.get("items") or []
       if i.get("metadata", {}).get("name") == sys.argv[1]]
if len(ids) > 1:
    sys.exit("several endpoints share the name " + sys.argv[1])
print(ids[0] if ids else "")' "$NAME" "$listing" || die "could not read the endpoint listing"
}

endpoint_field() {  # endpoint_field <id> url|image
  local spec
  spec="$(nebius ai endpoint get --id "$1" --format json)" || die "nebius ai endpoint get failed"
  "$PY" -c '
import json, sys
data = json.loads(sys.argv[2])
if sys.argv[1] == "image":
    print(data.get("spec", {}).get("image", ""))
    sys.exit()
for u in data.get("status", {}).get("public_endpoints", []):
    u = u if isinstance(u, str) else (u.get("url") or u.get("address") or "")
    if u:
        print(u if u.startswith("http") else "https://" + u)
        break' "$2" "$spec" || die "could not read endpoint $1"
}

https_url() {
  [[ "$1" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "the endpoint URL is not https: $1"
  printf '%s' "$1"
}

# --- changing the platform ----------------------------------------------------------------
undrain_on_failure() {  # an update that fails leaves the old instance serving again
  local status=$?
  [ "$status" -eq 0 ] && return
  echo "endpoint.sh: failed: un-draining $old_url" >&2
  printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" | curl -fsS --proto =https --max-time 20 -H @- \
    -H 'Content-Type: application/json' -d '{"on": false}' "$old_url/admin/drain" >/dev/null \
    || echo "endpoint.sh: un-drain failed too: POST /admin/drain {\"on\": false} by hand" >&2
}

drain() {  # new runs get 503 + Retry-After; running steps keep going until the stop
  if [ "${P2P_SKIP_DRAIN:-}" = 1 ]; then echo "drain skipped (rollback)"; return; fi
  [ -n "${ADMIN_TOKEN:-}" ] \
    || die "replacing the endpoint needs ADMIN_TOKEN to drain it (or P2P_SKIP_DRAIN=1 for a rollback)"
  if [ "${DRY_RUN:-}" = 1 ]; then echo "DRY-RUN: POST $old_url/admin/drain"; return; fi
  printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" | curl -fsS --proto =https --max-time 20 -H @- \
    -H 'Content-Type: application/json' -d '{"on": true}' "$old_url/admin/drain" >/dev/null \
    || die "drain failed (P2P_SKIP_DRAIN=1 if the instance is down)"
  echo "drained: waiting for running steps to reach a safe point"
  sleep "${P2P_DRAIN_WAIT_S:-60}"
}

wait_gone() {  # the name is free again before the new endpoint is created
  [ "${DRY_RUN:-}" = 1 ] && return
  for _ in $(seq 1 60); do
    [ -z "$(endpoint_id)" ] && return
    sleep 10
  done
  die "endpoint $NAME still listed 10 minutes after delete"
}

wait_ready() {  # up to 10 minutes for /readyz from this build (db, models, replication)
  [ "${DRY_RUN:-}" = 1 ] && { echo "DRY-RUN: wait for /readyz (commit ${commit:-any})"; return; }
  local id url body
  for _ in $(seq 1 "${P2P_READY_ATTEMPTS:-60}"); do
    id="$(endpoint_id)"
    url="$( [ -n "$id" ] && endpoint_field "$id" url || true)"
    if [ -n "$url" ] && body="$(curl -fsS --proto =https --max-time 10 "$(https_url "$url")/readyz" 2>/dev/null)" \
      && { [ -z "$commit" ] || [[ "$body" == *"\"commit\":\"$commit\""* ]]; }; then
      echo "ready at $url"
      return
    fi
    sleep 10
  done
  die "/readyz not ready with commit ${commit:-?} after 10 minutes: roll back with the previous digest (P2P_SKIP_DRAIN=1)"
}

create() {
  run nebius ai endpoint create --parent-id "$project" --name "$NAME" --image "$image" \
    --container-port 8080 --platform cpu-d3 --preset 2vcpu-8gb --disk-size 30Gi \
    --subnet-id "$subnet" --env "$env_arg" --env-secret "$secret_arg"
}

id="$(endpoint_id)"
if [ -z "$id" ]; then
  echo "creating $NAME with $image"
  create
else
  old_url="$(https_url "$(endpoint_field "$id" url)")"
  echo "current image: $(endpoint_field "$id" image) (the rollback target)"
  if [ "${P2P_ENDPOINT_UPDATE:-recreate}" = update ]; then
    drain
    trap undrain_on_failure EXIT
    echo "updating $NAME ($id) to $image"
    run nebius ai endpoint update --id "$id" --image "$image" --env "$env_arg" \
      --env-secret "$secret_arg"
  else
    drain
    echo "recreating $NAME ($id) with $image (~5 min downtime, one database writer at a time)"
    run nebius ai endpoint delete --id "$id"
    wait_gone
    create
  fi
fi
wait_ready
trap - EXIT
echo "deployed $image: record it; rolling back = this script with the previous digest"
