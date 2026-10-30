#!/usr/bin/env python3
"""Render the egress gateway's squid.conf from config/allowlist.yaml (architecture §8.2, A.2).

Runs on the gateway at first boot (Ubuntu 24.04: python3 and python3-yaml only) and in
tests/infra. Each source subnet gets its own SNI allowlist: a host listed only under `exec` never
reaches the provision ACL and the other way round. TLS is peeked, spliced when the SNI matches the
source subnet's list and terminated otherwise; nothing is ever decrypted.

    render_squid.py --allowlist allowlist.yaml --provision-cidr 10.1.0.0/24 \
        --exec-cidr 10.1.1.0/24 --var NEBIUS_STORAGE_HOST=... [--exec-hosts] [--output PATH]

`--exec-hosts` prints the concrete exec hostnames instead, one per line, for p2p-exec-pin.
"""

import argparse
import ipaddress
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

POLICIES = ("provision", "exec")
LOCAL_PORT = "127.0.0.1:3127"
HTTP_PORT = 3128
HTTPS_PORT = 3129
DUMMY_CERT = "/etc/squid/dummy.pem"

_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# A DNS name with at least two labels and an alphabetic TLD, optionally with a leading dot
# (Squid's "this domain and its subdomains"). No wildcards, IPs or whitespace.
_HOST = re.compile(rf"^\.?(?:{_LABEL}\.)+[a-z][a-z0-9-]*[a-z0-9]$")


class AllowlistError(ValueError):
    """The allowlist or the subnet CIDRs cannot be rendered safely."""


def _substitute(raw: str, variables: Mapping[str, str]) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise AllowlistError(f"no value for placeholder ${{{name}}} (pass --var {name}=...)")
        return variables[name]

    return _PLACEHOLDER.sub(value, raw)


def _check_overlap(policy: str, hosts: Sequence[str]) -> None:
    # Squid rejects or silently drops overlapping dstdomain/server_name entries.
    for wildcard in (h for h in hosts if h.startswith(".")):
        for host in hosts:
            if host != wildcard and (host == wildcard[1:] or host.endswith(wildcard)):
                raise AllowlistError(f"{policy}: {host!r} overlaps {wildcard!r}")


def load_policies(text: str, variables: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Hosts per policy from the allowlist YAML, placeholders substituted and validated."""
    data = yaml.safe_load(text)
    egress = data.get("egress") if isinstance(data, dict) else None
    if not isinstance(egress, dict):
        raise AllowlistError("allowlist has no 'egress' mapping")
    if set(egress) != set(POLICIES):
        raise AllowlistError(f"egress must have exactly the policies {', '.join(POLICIES)}")
    policies: dict[str, tuple[str, ...]] = {}
    for policy in POLICIES:
        entries = egress[policy] or []
        if not isinstance(entries, list):
            raise AllowlistError(f"egress.{policy} must be a list")
        hosts: list[str] = []
        for raw in entries:
            if not isinstance(raw, str):
                raise AllowlistError(f"{policy}: invalid entry {raw!r}")
            host = _substitute(raw, variables).strip().lower()
            if not _HOST.fullmatch(host):
                raise AllowlistError(f"{policy}: invalid host {host!r}")
            if host not in hosts:
                hosts.append(host)
        _check_overlap(policy, hosts)
        policies[policy] = tuple(hosts)
    return policies


def _subnets(provision_cidr: str, exec_cidr: str) -> dict[str, ipaddress.IPv4Network]:
    subnets: dict[str, ipaddress.IPv4Network] = {}
    for policy, cidr in zip(POLICIES, (provision_cidr, exec_cidr), strict=True):
        try:
            net = ipaddress.ip_network(cidr, strict=True)
        except ValueError as exc:
            raise AllowlistError(f"{policy}: invalid CIDR {cidr!r}: {exc}") from exc
        if not isinstance(net, ipaddress.IPv4Network) or not net.is_private:
            raise AllowlistError(f"{policy}: {cidr!r} is not a private IPv4 range")
        subnets[policy] = net
    if subnets["provision"].overlaps(subnets["exec"]):
        raise AllowlistError("provision and exec CIDRs overlap")
    return subnets


def render_squid_conf(
    policies: Mapping[str, Sequence[str]], provision_cidr: str, exec_cidr: str
) -> str:
    """squid.conf text: intercept ports, per-subnet ACLs, peek/splice/terminate."""
    subnets = _subnets(provision_cidr, exec_cidr)
    active = [p for p in POLICIES if policies[p]]
    lines = [
        "# Generated at first boot by p2p-render-squid (infra/cloud-init/render_squid.py)",
        "# from /etc/p2p/allowlist.yaml (config/allowlist.yaml). Do not edit by hand.",
        "",
        "# Loopback forward-proxy port: Squid wants one; http_access allows nothing on it.",
        f"http_port {LOCAL_PORT}",
        "# nftables redirects job traffic here (tcp/80 and tcp/443 from provision and exec).",
        f"http_port {HTTP_PORT} intercept name=http_intercept",
        f"https_port {HTTPS_PORT} intercept ssl-bump tls-cert={DUMMY_CERT}"
        " generate-host-certificates=off name=https_intercept",
        "",
        "acl port_http myportname http_intercept",
        "acl port_https myportname https_intercept",
        "acl step1 at_step SslBump1",
        "acl CONNECT method CONNECT",
        "acl private_dst dst 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10"
        " 127.0.0.0/8 169.254.0.0/16",
    ]
    for policy in POLICIES:
        lines.append(f"acl {policy}_src src {subnets[policy]}")
    for policy in active:
        hosts = " ".join(policies[policy])
        lines.append(f"acl allow_{policy} ssl::server_name {hosts}")
        lines.append(f"acl allow_{policy}_http dstdomain {hosts}")
    lines += [
        "",
        "# HTTPS: the fake CONNECT of step 1 must pass http_access; ssl_bump then decides by SNI.",
        "# Plain HTTP: the Host header must be on the source subnet's list, and CONNECT (a blind",
        "# tunnel) is never allowed there.",
        "http_access deny port_http CONNECT",
        "# An allowlisted name never reaches a private address (other sandbox hosts).",
        "http_access deny private_dst",
    ]
    lines += [f"http_access allow port_https {p}_src" for p in active]
    lines += [f"http_access allow port_http {p}_src allow_{p}_http" for p in active]
    lines += [
        "http_access deny all",
        "",
        "ssl_bump peek step1",
        *(f"ssl_bump splice {p}_src allow_{p}" for p in active),
        "ssl_bump terminate all",
        "# Non-TLS traffic on 443 gets an error, never a blind tunnel.",
        "on_unsupported_protocol respond all",
        "",
        "# CDNs (Hugging Face, PyPI) resolve differently for the job and for Squid: a strict check",
        "# would reject them. Squid then connects only to the original destination (residual",
        "# risk in infra/README.md; exec destinations are pinned by nftables).",
        "host_verify_strict off",
        "",
        "cache deny all",
        "cache_mem 16 MB",
        "via off",
        "forwarded_for delete",
        "httpd_suppress_version_string on",
        "pinger_enable off",
        "visible_hostname p2p-egress-gw",
        "shutdown_lifetime 3 seconds",
        "coredump_dir /var/spool/squid",
        "",
        "# One line per connection, allowed or denied, to journald (Nebius Observability Logs).",
        "# The client-controlled fields (SNI, Host) come last and quoted, so they cannot forge",
        "# the fields before them.",
        "logformat p2p client=%>a:%>p dst=%>la:%>lp server=%<a:%<p result=%Ss/%>Hs"
        ' bump=%ssl::bump_mode req_bytes=%>st resp_bytes=%<st sni=%"ssl::>sni host=%"{Host}>h',
        "access_log syslog:daemon.info p2p",
        "",
    ]
    return "\n".join(lines)


def exec_hosts(policies: Mapping[str, Sequence[str]]) -> list[str]:
    """Concrete exec hostnames, whose addresses nftables pins as the only exec destinations."""
    wildcards = [h for h in policies["exec"] if h.startswith(".")]
    if wildcards:
        raise AllowlistError(f"exec entries must be concrete hosts to be pinned: {wildcards}")
    return list(policies["exec"])


def _write(path: Path, text: str) -> None:
    """Atomic replace, so a failed render never leaves a half-written file behind."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--provision-cidr", required=True)
    parser.add_argument("--exec-cidr", required=True)
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--exec-hosts", action="store_true", help="print the exec hostnames")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        variables = dict(item.split("=", 1) for item in args.var)
        policies = load_policies(args.allowlist.read_text(encoding="utf-8"), variables)
        if args.exec_hosts:
            _subnets(args.provision_cidr, args.exec_cidr)
            text = "".join(f"{host}\n" for host in exec_hosts(policies))
        else:
            text = render_squid_conf(policies, args.provision_cidr, args.exec_cidr)
    except (AllowlistError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"render_squid: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        sys.stdout.write(text)
    else:
        _write(args.output, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
