#!/bin/sh
# Endpoint start (A-14, architecture ADR-002): the container disk is ephemeral, so the SQLite
# database is restored from its Litestream replica, migrated, then served by uvicorn as a child
# of `litestream replicate`, which replicates every write to the p2p-state bucket. A failure in
# any step stops the start, and a missing replica is an error: an empty database would reset
# the ledger, the daily caps and the kill switch. Only the very first deploy sets
# P2P_DB_BOOTSTRAP=1 to start from an empty database.
set -eu

: "${P2P_STATE_BUCKET:?P2P_STATE_BUCKET is required (the Litestream replica bucket)}"
: "${S3_ENDPOINT:?S3_ENDPOINT is required (never fall back to another S3 provider)}"
: "${RUNTIME_S3_KEY_ID:?RUNTIME_S3_KEY_ID is required (S3 access key of sa-api-runtime)}"
: "${RUNTIME_S3_SECRET:?RUNTIME_S3_SECRET is required (S3 secret of sa-api-runtime)}"
case "$S3_ENDPOINT" in
  https://*) ;;
  *) echo "S3_ENDPOINT must be an https:// URL" >&2; exit 1 ;;
esac
config="${P2P_LITESTREAM_CONFIG:-/opt/p2p/litestream.yml}"
db=/var/lib/p2p/p2p.db

# litestream reads its credentials from these (never from the config file or the command line)
export LITESTREAM_ACCESS_KEY_ID="$RUNTIME_S3_KEY_ID"
export LITESTREAM_SECRET_ACCESS_KEY="$RUNTIME_S3_SECRET"
export S3_REGION="${S3_REGION:-eu-north1}"

if [ "${P2P_DB_BOOTSTRAP:-}" = 1 ]; then
  echo "P2P_DB_BOOTSTRAP=1: an empty database is allowed if there is no replica yet" >&2
  litestream restore -config "$config" -if-replica-exists -if-db-not-exists "$db"
else
  litestream restore -config "$config" -if-db-not-exists "$db"
fi
alembic upgrade head

# Behind the managed endpoint's proxy: trust forwarded client addresses only from the proxy's
# addresses (measured in H-05), never from everywhere.
proxy=""
if [ -n "${P2P_FORWARDED_ALLOW_IPS:-}" ]; then
  case "$P2P_FORWARDED_ALLOW_IPS" in
    *[!0-9a-fA-F.:/,]*|"*") echo "P2P_FORWARDED_ALLOW_IPS must list proxy IPs or CIDRs" >&2; exit 1 ;;
  esac
  proxy=" --proxy-headers --forwarded-allow-ips=$P2P_FORWARDED_ALLOW_IPS"
fi

export P2P_REPLICATION=litestream  # with the parent process check, what /readyz reports
# A bounded graceful shutdown: open SSE streams must not hold litestream's final sync past the
# platform's kill.
exec litestream replicate -config "$config" -exec \
  "uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --no-server-header --timeout-graceful-shutdown 10$proxy"
