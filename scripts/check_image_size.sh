#!/usr/bin/env bash
# Fail when an image is larger than its budget, measured compressed (gzip -1 of `docker save`,
# close to what the registry stores): runner <= 8 GB, runner-cpu <= 2.5 GB (A-06; pull time
# matters for every job, see spike S-12).
# Usage: scripts/check_image_size.sh [tag]   (default tag: dev)
set -euo pipefail
tag="${1:-dev}"
status=0
check() {  # image limit_bytes
  local bytes
  bytes="$(docker save "$1:$tag" | gzip -1 | wc -c)"
  awk -v n="$1" -v b="$bytes" -v l="$2" \
    'BEGIN { printf "%-16s %6.2f GB compressed (limit %.2f GB)\n", n, b / 1e9, l / 1e9 }'
  if [ "$bytes" -gt "$2" ]; then
    echo "  FAIL: $1 is over its size budget" >&2
    status=1
  fi
}
check p2p-runner 8000000000
check p2p-runner-cpu 2500000000
exit "$status"
