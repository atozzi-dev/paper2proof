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

# Behind the managed endpoint's proxy: trust forwarded client addresses only from the proxy's
# addresses (measured in H-05), never from everywhere. Several CIDRs are separated by ";"
# (commas separate the endpoint's environment variables). Each one is parsed as uvicorn will
# parse it (ipaddress, strict), so "/00", host bits or two halves of the Internet are refused.
proxy=""
if [ -n "${P2P_FORWARDED_ALLOW_IPS:-}" ]; then
  bad_proxy="P2P_FORWARDED_ALLOW_IPS must list the proxy's IPs or CIDRs (at most /8 or /32 wide)"
  case "$P2P_FORWARDED_ALLOW_IPS" in
    *[!0-9a-fA-F.:/\;]*) echo "$bad_proxy" >&2; exit 1 ;;
  esac
  python -I -c '
import ipaddress, sys
for part in sys.argv[1].split(";"):
    net = ipaddress.ip_network(part)
    if net.prefixlen < (8 if net.version == 4 else 32):
        sys.exit(1)
' "$P2P_FORWARDED_ALLOW_IPS" 2> /dev/null || { echo "$bad_proxy" >&2; exit 1; }
  proxy=" --proxy-headers --forwarded-allow-ips=$(printf '%s' "$P2P_FORWARDED_ALLOW_IPS" | tr ';' ',')"
fi
config="${P2P_LITESTREAM_CONFIG:-/opt/p2p/litestream.yml}"
db=/var/lib/p2p/p2p.db
# The app must open the very file litestream restores and replicates: anything else would keep
# the ledger, the daily caps and the kill switch in an unreplicated file (a host checkout).
if [ "${DB_URL:-}" != "sqlite+aiosqlite:///$db" ]; then
  echo "DB_URL must be sqlite+aiosqlite:///$db (the database litestream replicates)" >&2
  exit 1
fi
# 0.0.0.0 behind the managed endpoint; 127.0.0.1 on a host of ours behind its TLS proxy (the
# hybrid deploy). An IP literal only, so nothing else reaches uvicorn's command line.
bind="${P2P_BIND_HOST:-0.0.0.0}"
python -I -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "$bind" 2> /dev/null || {
  echo "P2P_BIND_HOST must be an IP address" >&2
  exit 1
}

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

export P2P_REPLICATION=litestream  # with the parent process check, what /readyz reports
# A bounded graceful shutdown: open SSE streams must not hold litestream's final sync past the
# platform's kill.
exec litestream replicate -config "$config" -exec \
  "uvicorn app.main:app --host $bind --port 8080 --workers 1 --no-server-header --timeout-graceful-shutdown 10$proxy"
