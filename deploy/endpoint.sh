#!/usr/bin/env bash
# Deploy the control plane (A-14, architecture §11.1): create the p2p-api Serverless endpoint,
# or replace it with a new image digest after draining and stopping the running instance.
# Human step (H-05) or the CI deploy job (A-05); never run it from an agent.
#
# Usage: deploy/endpoint.sh [--bootstrap] <registry/p2p-api@sha256:...> [env-file]
#   --bootstrap  first deploy only: the database may start empty (no Litestream replica yet).
#                Refused when the endpoint exists; redeploy without it right afterwards.
#   env-file     non-secret KEY=VALUE lines for the app (default deploy/prod.env), only the keys
#                of deploy/prod.env.example; secrets come from the Nebius secret named by
#                P2P_SECRET_NAME (loaded out of band), never from here.
# Environment:
#   P2P_CONTROL_PROJECT_ID the p2p-control project (required: never the CLI's default)
#   SUBNET_CONTROL_ID      its subnet (required: checked before anything is deleted)
#   P2P_EXPECT_COMMIT      the git commit baked into the image (required: /readyz must report it)
#   ADMIN_TOKEN            admin token for POST /admin/drain (sent on stdin, never argv)
#   P2P_ENDPOINT_UPDATE    "recreate" (default: the old instance is stopped and deleted before
#                          the new one starts, so there is one database writer) or "update"
#                          once S-6 shows that an in-place update stops the old instance first
#   P2P_SKIP_DRAIN=1       rollback of an unresponsive instance (nothing to drain)
#   DRY_RUN=1              print the mutating nebius commands instead of running them
# The endpoint URL, image and state are read from `nebius ai endpoint get`. Assumptions to
# re-check with spike S-6 (H-05 checklist in docs/DEPLOY.md): update semantics and overlap,
# list paging, the --env/--env-secret syntax and the field names of `endpoint get`.
set -euo pipefail

NAME="${P2P_ENDPOINT_NAME:-p2p-api}"
SECRET="${P2P_SECRET_NAME:-p2p-secrets}"
PY="${PY:-python3}"
SECRET_VARS=(NEBIUS_API_KEY TAVILY_API_KEY SESSION_KEY ADMIN_TOKEN RUNTIME_S3_KEY_ID
  RUNTIME_S3_SECRET)  # the job launcher's credentials are added with the Nebius adapters (A-20)
ENV_KEYS=" S3_ENDPOINT S3_REGION P2P_STATE_BUCKET P2P_CONTROL_BUCKET NEBIUS_SANDBOX_PROJECT_ID
  P2P_PROVISIONER_IMAGE P2P_RUNNER_IMAGE P2P_RUNNER_CPU_IMAGE P2P_SUBNET_PROVISION
  P2P_SUBNET_EXEC P2P_GPU_PREEMPTIBLE P2P_BUNDLE_KEY_ID P2P_FORWARDED_ALLOW_IPS "
# nothing else: no P2P_ENV, DB_URL, SMOKE_EXECUTOR, P2P_DB_BOOTSTRAP or build commit overrides

die() { echo "endpoint.sh: $*" >&2; exit 1; }
run() {
  if [ "${DRY_RUN:-}" = 1 ]; then printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

# --- every input is checked before anything changes ---------------------------------------
bootstrap=0
if [ "${1:-}" = --bootstrap ]; then bootstrap=1; shift; fi
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
commit="${P2P_EXPECT_COMMIT:-}"
[[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] \
  || die "P2P_EXPECT_COMMIT is required (the git commit the image was built from)"

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

endpoint_field() {  # endpoint_field <id> url|image|state
  local spec
  spec="$(nebius ai endpoint get --id "$1" --format json)" || die "nebius ai endpoint get failed"
  "$PY" -c '
import json, sys
data = json.loads(sys.argv[2])
field, status = sys.argv[1], data.get("status", {})
if field == "image":
    print(data.get("spec", {}).get("image", ""))
elif field == "state":
    print(status.get("state") or status.get("phase") or "")
else:
    for u in status.get("public_endpoints", []):
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
drained=0
undrain_on_failure() {  # a replace that fails before the old instance is gone: serve again
  local status=$?
  [ "$status" -eq 0 ] || [ "$drained" -eq 0 ] && return
  echo "endpoint.sh: failed: un-draining $old_url" >&2
  printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" | curl -fsS --proto =https --max-time 20 -H @- \
    -H 'Content-Type: application/json' -d '{"on": false}' "$old_url/admin/drain" >/dev/null \
    || echo "endpoint.sh: un-drain failed too: POST /admin/drain {\"on\": false} by hand" >&2
}

drain() {  # new runs get 503 + Retry-After; running steps reach a safe point
  if [ "${P2P_SKIP_DRAIN:-}" = 1 ]; then echo "drain skipped (rollback)"; return; fi
  [ -n "${ADMIN_TOKEN:-}" ] \
    || die "replacing the endpoint needs ADMIN_TOKEN to drain it (or P2P_SKIP_DRAIN=1 for a rollback)"
  old_url="$(https_url "$(endpoint_field "$id" url)")"
  if [ "${DRY_RUN:-}" = 1 ]; then echo "DRY-RUN: POST $old_url/admin/drain"; return; fi
  trap undrain_on_failure EXIT
  printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" | curl -fsS --proto =https --max-time 20 -H @- \
    -H 'Content-Type: application/json' -d '{"on": true}' "$old_url/admin/drain" >/dev/null \
    || die "drain failed (P2P_SKIP_DRAIN=1 if the instance is down)"
  drained=1
  echo "drained: waiting for running steps to reach a safe point"
  sleep "${P2P_DRAIN_WAIT_S:-60}"
}

wait_state() {  # wait_state <id> <state>: up to 10 minutes
  [ "${DRY_RUN:-}" = 1 ] && return
  local state
  for _ in $(seq 1 "${P2P_WAIT_ATTEMPTS:-60}"); do
    state="$(endpoint_field "$1" state)" || exit 1
    [ "$state" = "$2" ] && return
    sleep 10
  done
  die "endpoint $1 not $2 after 10 minutes (last state: ${state:-?})"
}

wait_gone() {  # the name is free again before the new endpoint is created
  [ "${DRY_RUN:-}" = 1 ] && return
  local listed
  for _ in $(seq 1 "${P2P_WAIT_ATTEMPTS:-60}"); do
    listed="$(endpoint_id)" || exit 1  # a failed listing is never "gone"
    [ -z "$listed" ] && return
    sleep 10
  done
  die "endpoint $NAME still listed 10 minutes after delete"
}

wait_ready() {  # up to 10 minutes for /readyz from this build (db, models, replication)
  [ "${DRY_RUN:-}" = 1 ] && { echo "DRY-RUN: wait for /readyz (commit $commit)"; return; }
  local found url body
  for _ in $(seq 1 "${P2P_READY_ATTEMPTS:-60}"); do
    url=""
    if found="$(endpoint_id 2>/dev/null)" && [ -n "$found" ]; then
      url="$(endpoint_field "$found" url 2>/dev/null)" || url=""
    fi  # a transient listing error is retried here, nothing is created again
    if [ -n "$url" ] && body="$(curl -fsS --proto =https --max-time 10 "$(https_url "$url")/readyz" 2>/dev/null)" \
      && [[ "$body" == *"\"commit\":\"$commit\""* ]]; then
      echo "ready at $url"
      return
    fi
    sleep 10
  done
  die "/readyz not ready with commit $commit after 10 minutes: roll back with the previous digest (P2P_SKIP_DRAIN=1)"
}

create() {
  local env_create="$env_arg"
  [ "$bootstrap" = 1 ] && env_create="$env_create,P2P_DB_BOOTSTRAP=1"
  run nebius ai endpoint create --parent-id "$project" --name "$NAME" --image "$image" \
    --container-port 8080 --platform cpu-d3 --preset 2vcpu-8gb --disk-size 30Gi \
    --subnet-id "$subnet" --env "$env_create" --env-secret "$secret_arg"
}

id="$(endpoint_id)"
if [ -z "$id" ]; then
  echo "creating $NAME with $image$([ "$bootstrap" = 1 ] && echo " (bootstrap: empty database allowed)")"
  create
else
  [ "$bootstrap" = 0 ] || die "--bootstrap is only for the first deploy: $NAME exists ($id)"
  current="$(endpoint_field "$id" image)"
  [ -n "$current" ] || echo "endpoint.sh: WARNING: could not read the current image (rollback target)" >&2
  echo "current image: ${current:-?} (the rollback target)"
  drain
  if [ "${P2P_ENDPOINT_UPDATE:-recreate}" = update ]; then
    echo "updating $NAME ($id) to $image"
    run nebius ai endpoint update --id "$id" --image "$image" --env "$env_arg" \
      --env-secret "$secret_arg"
  else
    echo "recreating $NAME ($id) with $image (~5 min downtime, one database writer at a time)"
    run nebius ai endpoint stop --id "$id"  # Litestream's final sync before anything new starts
    wait_state "$id" STOPPED
    run nebius ai endpoint delete --id "$id"
    drained=0  # the old instance is gone: nothing left to un-drain
    wait_gone
    create
  fi
fi
wait_ready
trap - EXIT
echo "deployed $image: record it; rolling back = this script with the previous digest"
