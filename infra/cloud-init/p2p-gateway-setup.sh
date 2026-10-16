#!/bin/sh
# First-boot setup of the egress gateway, run once by cloud-init (runcmd) after the packages are
# installed. It stops at the first error: Squid then keeps the package's deny-all configuration,
# nothing is redirected and, with forwarding off, jobs have no egress (fail-closed).
set -eu

# Progress and failure go to the serial console too: nobody can log in to look.
console() {
	echo "p2p-gateway-setup: $*"
	echo "p2p-gateway-setup: $*" >/dev/console 2>/dev/null || true
}
on_exit() {
	status=$?
	if [ "$status" -ne 0 ]; then
		console "FAILED (exit $status): the gateway is not serving; replace the VM"
	fi
}
trap on_exit EXIT

# cloud-init does not retry a failed package install (mirror outage): retry here.
for attempt in 1 2 3 4 5; do
	if command -v squid >/dev/null && command -v nft >/dev/null; then
		break
	fi
	console "packages missing, install attempt $attempt"
	apt-get update -q || true
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q squid-openssl nftables python3-yaml openssl || sleep 30
done
command -v squid >/dev/null && command -v nft >/dev/null

# shellcheck source=/dev/null
. /etc/p2p/gateway.env

# No logins: the gateway is reached only through the serial console of a rebuilt VM.
# Masked, so a package upgrade cannot bring it back.
systemctl disable --now ssh.socket ssh.service 2>/dev/null || true
systemctl mask ssh.socket ssh.service

sysctl --system >/dev/null
test "$(sysctl -n net.ipv4.ip_forward)" = 0
systemctl restart systemd-journald.service

systemctl enable nftables.service
systemctl restart nftables.service

# Squid needs a certificate on an ssl-bump port even though it only peeks and splices. The key is
# made here and never leaves the VM.
if [ ! -s /etc/squid/dummy.pem ]; then
	(
		umask 077
		openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
			-subj /CN=p2p-egress-gateway -keyout /etc/squid/dummy.pem -out /etc/squid/dummy.pem \
			2>/dev/null
	)
fi
chgrp proxy /etc/squid/dummy.pem
chmod 0640 /etc/squid/dummy.pem

render() {
	/usr/local/sbin/p2p-render-squid --allowlist /etc/p2p/allowlist.yaml \
		--provision-cidr "$P2P_PROVISION_CIDR" --exec-cidr "$P2P_EXEC_CIDR" \
		--var "NEBIUS_STORAGE_HOST=$NEBIUS_STORAGE_HOST" \
		--var "NEBIUS_REGISTRY_HOST=$NEBIUS_REGISTRY_HOST" "$@"
}
render --output /etc/squid/squid.conf
render --exec-hosts --output /etc/p2p/exec-hosts.txt
squid -k parse -f /etc/squid/squid.conf

systemctl daemon-reload
systemctl enable --now p2p-exec-pin.timer
systemctl start p2p-exec-pin.service
systemctl enable squid.service
systemctl restart squid.service
console "egress gateway ready"
