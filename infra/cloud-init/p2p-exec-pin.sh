#!/bin/sh
# Refresh the nftables set inet p2p exec_dst4 with the IPv4 addresses of the exec allowlist
# (one hostname per line in /etc/p2p/exec-hosts.txt, written by p2p-render-squid). Run by
# p2p-exec-pin.timer every minute. Elements expire 15 minutes after they were last seen, so the
# rotating address subsets of round-robin endpoints accumulate, and a host that fails to resolve
# keeps its recent addresses for a while instead of disappearing at once. exec never gains an
# address its hosts did not resolve to. Output goes to journald through the service's stdout.
set -eu

hosts_file=${1:-/etc/p2p/exec-hosts.txt}
ttl=${P2P_PIN_TTL:-15m}

addrs=$(
	while IFS= read -r host || [ -n "$host" ]; do
		case "$host" in
			'' | '#'*) continue ;;
		esac
		if ! answer=$(getent ahostsv4 "$host"); then
			echo "p2p-exec-pin: $host did not resolve" >&2
			continue
		fi
		printf '%s\n' "$answer" | awk '{ print $1 }'
	done <"$hosts_file" | sort -u
)

if [ -z "$addrs" ]; then
	echo "p2p-exec-pin: no address resolved; exec_dst4 left as it is"
	exit 0
fi

# add + delete + add in one transaction: new addresses are added and known ones get a fresh
# timeout (re-adding an existing element alone does not refresh it).
elements=$(printf '%s\n' "$addrs" | sed "s/\$/ timeout $ttl/" | paste -sd, -)
plain=$(printf '%s\n' "$addrs" | paste -sd, -)
nft -f - <<EOF
add element inet p2p exec_dst4 { $plain }
delete element inet p2p exec_dst4 { $plain }
add element inet p2p exec_dst4 { $elements }
EOF
echo "p2p-exec-pin: exec_dst4 refreshed with { $plain }"
