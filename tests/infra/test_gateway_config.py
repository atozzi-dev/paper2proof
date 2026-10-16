"""Egress gateway configuration (A-04, architecture §8.2 and appendix A.2).

Host-side tests render squid.conf with infra/cloud-init/render_squid.py (the same script the
gateway runs at first boot) and the Terraform templates with the variables the module passes.
The `docker` tests check the rendered files with the real tools on Ubuntu 24.04:
`squid -k parse`, `nft -c -f` and `cloud-init schema` (`uv run pytest -m docker tests/infra`).
"""

import base64
import importlib.util
import ipaddress
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLOUD_INIT = ROOT / "infra" / "cloud-init"
MODULE = ROOT / "infra" / "terraform" / "modules" / "egress_gateway"
PROD = ROOT / "infra" / "terraform" / "envs" / "prod"
ALLOWLIST = ROOT / "config" / "allowlist.yaml"

PROVISION_CIDR = "10.1.0.0/24"
EXEC_CIDR = "10.1.1.0/24"
GATEWAY_IP = "10.1.2.10"
STORAGE_HOST = "storage.eu-north1.nebius.cloud"
REGISTRY_HOST = "cr.eu-north1.nebius.cloud"
VARIABLES = {"NEBIUS_STORAGE_HOST": STORAGE_HOST, "NEBIUS_REGISTRY_HOST": REGISTRY_HOST}

TEST_IMAGE = "p2p-gateway-test:dev"


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_squid", CLOUD_INIT / "render_squid.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load_renderer()


def _render(allowlist: str | None = None) -> str:
    text = ALLOWLIST.read_text(encoding="utf-8") if allowlist is None else allowlist
    policies = rs.load_policies(text, VARIABLES)
    return str(rs.render_squid_conf(policies, PROVISION_CIDR, EXEC_CIDR))


def _directives(conf: str) -> list[str]:
    return [line.strip() for line in conf.splitlines() if line.strip() and line[0] != "#"]


def _acl(conf: str, name: str) -> list[list[str]]:
    """Values of every `acl <name> <type> ...` line, one list per line."""
    return [line.split()[2:] for line in _directives(conf) if line.split()[:2] == ["acl", name]]


def _expected_hosts(policy: str) -> list[str]:
    raw = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8"))["egress"][policy]
    out = []
    for host in raw:
        for name, value in VARIABLES.items():
            host = host.replace("${" + name + "}", value)
        out.append(host)
    return out


# --- render_squid.py: per-subnet SNI allowlists ---------------------------------------------


def test_one_provision_acl_line_holds_every_provision_host() -> None:
    lines = _acl(_render(), "allow_provision")
    assert len(lines) == 1
    assert lines[0][0] == "ssl::server_name"
    assert lines[0][1:] == _expected_hosts("provision")


def test_the_exec_acl_holds_exactly_the_exec_hosts() -> None:
    lines = _acl(_render(), "allow_exec")
    assert len(lines) == 1
    assert lines[0] == ["ssl::server_name", *_expected_hosts("exec")]


def test_an_exec_only_host_never_leaks_into_provision() -> None:
    allowlist = """
egress:
  provision: [.pypi.org, github.com]
  exec: [exec-only.example.net, "${NEBIUS_STORAGE_HOST}"]
"""
    conf = _render(allowlist)
    provision_lines = [
        line for line in _directives(conf) if "provision" in line and line.startswith("acl")
    ]
    assert provision_lines, conf
    assert not any("exec-only.example.net" in line for line in provision_lines)
    assert not any(STORAGE_HOST in line for line in provision_lines)
    assert _acl(conf, "allow_exec") == [["ssl::server_name", "exec-only.example.net", STORAGE_HOST]]
    assert _acl(conf, "allow_exec_http") == [["dstdomain", "exec-only.example.net", STORAGE_HOST]]


def test_plain_http_uses_the_same_per_subnet_lists() -> None:
    conf = _render()
    assert _acl(conf, "allow_provision_http") == [["dstdomain", *_expected_hosts("provision")]]
    assert _acl(conf, "allow_exec_http") == [["dstdomain", *_expected_hosts("exec")]]


def test_source_subnets_come_from_the_cidrs() -> None:
    conf = _render()
    assert _acl(conf, "provision_src") == [["src", PROVISION_CIDR]]
    assert _acl(conf, "exec_src") == [["src", EXEC_CIDR]]


def test_ssl_bump_peeks_then_splices_by_subnet_and_terminates_the_rest() -> None:
    bump = [line for line in _directives(_render()) if line.startswith("ssl_bump ")]
    assert bump == [
        "ssl_bump peek step1",
        "ssl_bump splice provision_src allow_provision",
        "ssl_bump splice exec_src allow_exec",
        "ssl_bump terminate all",
    ]


def test_http_access_is_per_port_and_ends_with_deny_all() -> None:
    access = [line for line in _directives(_render()) if line.startswith("http_access ")]
    assert access[-1] == "http_access deny all"
    # Plain HTTP is filtered by Host per subnet; HTTPS passes to ssl_bump, which filters by SNI.
    assert "http_access allow port_http provision_src allow_provision_http" in access
    assert "http_access allow port_http exec_src allow_exec_http" in access
    assert "http_access allow port_https provision_src" in access
    assert "http_access allow port_https exec_src" in access
    allows = [line for line in access if line.startswith("http_access allow")]
    assert all("port_http" in line for line in allows)  # never the local forward-proxy port
    # Allowlisted names never reach private addresses (other sandbox hosts, metadata).
    assert access.index("http_access deny private_dst") < access.index(allows[0])
    assert _acl(_render(), "private_dst")[0][1:] == [
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
        "127.0.0.0/8", "169.254.0.0/16",
    ]  # fmt: skip
    # A CONNECT on the plain-HTTP port would be a blind tunnel: denied before any allow.
    assert access.index("http_access deny port_http CONNECT") < access.index(allows[0])


def test_client_controlled_log_fields_are_quoted_and_last() -> None:
    (logformat,) = [line for line in _directives(_render()) if line.startswith("logformat ")]
    assert logformat.endswith('sni=%"ssl::>sni host=%"{Host}>h')
    assert logformat.index("result=") < logformat.index("sni=")


def test_intercept_ports_and_tls_options() -> None:
    lines = _directives(_render())
    assert "http_port 3128 intercept name=http_intercept" in lines
    https = [line for line in lines if line.startswith("https_port ")]
    assert len(https) == 1
    opts = https[0].split()
    assert opts[1:4] == ["3129", "intercept", "ssl-bump"]
    assert "tls-cert=/etc/squid/dummy.pem" in opts
    assert "generate-host-certificates=off" in opts  # nothing is ever decrypted
    assert "host_verify_strict off" in lines
    assert "on_unsupported_protocol respond all" in lines  # non-TLS on 443 is never tunnelled
    assert "cache deny all" in lines
    assert any(line.startswith("access_log syslog:") for line in lines)  # -> journald


def test_the_local_forward_port_listens_on_loopback_only() -> None:
    ports = [line for line in _directives(_render()) if line.startswith("http_port ")]
    assert {p.split()[1] for p in ports} == {"127.0.0.1:3127", "3128"}


def test_an_empty_exec_policy_gets_no_acl_and_no_splice() -> None:
    conf = _render("egress:\n  provision: [.pypi.org]\n  exec: []\n")
    assert _acl(conf, "allow_exec") == []
    assert "ssl_bump splice exec_src allow_exec" not in _directives(conf)
    assert "http_access allow port_https exec_src" not in _directives(conf)
    assert "ssl_bump terminate all" in _directives(conf)


@pytest.mark.parametrize(
    ("allowlist", "message"),
    [
        ("egress:\n  provision: ['${UNKNOWN_HOST}']\n  exec: []\n", "UNKNOWN_HOST"),
        ("egress:\n  provision: ['pypi.org; http_access allow all']\n  exec: []\n", "invalid"),
        ("egress:\n  provision: ['*.pypi.org']\n  exec: []\n", "invalid"),
        ("egress:\n  provision: [localhost]\n  exec: []\n", "invalid"),
        ("egress:\n  provision: ['10.0.0.1']\n  exec: []\n", "invalid"),
        ("egress:\n  provision: [.pypi.org, files.pypi.org]\n  exec: []\n", "overlap"),
        ("egress:\n  provision: [.pypi.org, pypi.org]\n  exec: []\n", "overlap"),
        ("egress:\n  provision: [.pypi.org]\n", "provision, exec"),
        ("egress:\n  provision: [.pypi.org]\n  exec: []\n  other: []\n", "provision, exec"),
        ("egress:\n  provision: .pypi.org\n  exec: []\n", "list"),
        ("packages: {}\n", "egress"),
    ],
)
def test_bad_allowlists_fail_loudly(allowlist: str, message: str) -> None:
    with pytest.raises(rs.AllowlistError, match=re.escape(message)):
        _render(allowlist)


def test_a_bad_substituted_value_is_rejected() -> None:
    with pytest.raises(rs.AllowlistError, match="invalid"):
        rs.load_policies(
            "egress:\n  provision: ['${NEBIUS_STORAGE_HOST}']\n  exec: []\n",
            {"NEBIUS_STORAGE_HOST": "evil.example.com all"},
        )


def test_duplicates_are_dropped_and_case_is_normalised() -> None:
    policies = rs.load_policies(
        "egress:\n  provision: [GitHub.com, github.com]\n  exec: []\n", VARIABLES
    )
    assert policies["provision"] == ("github.com",)


@pytest.mark.parametrize(
    ("provision", "exec_"),
    [
        ("10.1.0.0/33", EXEC_CIDR),
        ("10.1.0.1/24", EXEC_CIDR),  # host bits set
        ("10.1.0.0/24", "10.1.0.0/25"),  # overlap
        ("fd00::/64", EXEC_CIDR),
        ("0.0.0.0/0", EXEC_CIDR),
    ],
)
def test_bad_cidrs_are_rejected(provision: str, exec_: str) -> None:
    policies = rs.load_policies(ALLOWLIST.read_text(encoding="utf-8"), VARIABLES)
    with pytest.raises(rs.AllowlistError):
        rs.render_squid_conf(policies, provision, exec_)


def test_exec_hosts_are_the_concrete_exec_names() -> None:
    policies = rs.load_policies(ALLOWLIST.read_text(encoding="utf-8"), VARIABLES)
    assert rs.exec_hosts(policies) == [STORAGE_HOST, REGISTRY_HOST]


def test_a_wildcard_exec_entry_cannot_be_pinned() -> None:
    policies = rs.load_policies("egress:\n  provision: []\n  exec: [.example.net]\n", VARIABLES)
    with pytest.raises(rs.AllowlistError, match="pinned"):
        rs.exec_hosts(policies)


def test_the_cli_writes_both_outputs(tmp_path: Path) -> None:
    squid_conf = tmp_path / "squid.conf"
    hosts = tmp_path / "exec-hosts.txt"
    common = [
        "--allowlist", str(ALLOWLIST),
        "--provision-cidr", PROVISION_CIDR,
        "--exec-cidr", EXEC_CIDR,
        "--var", f"NEBIUS_STORAGE_HOST={STORAGE_HOST}",
        "--var", f"NEBIUS_REGISTRY_HOST={REGISTRY_HOST}",
    ]  # fmt: skip
    assert rs.main([*common, "--output", str(squid_conf)]) == 0
    assert rs.main([*common, "--exec-hosts", "--output", str(hosts)]) == 0
    assert squid_conf.read_text(encoding="utf-8") == _render()
    assert hosts.read_text(encoding="utf-8").split() == [STORAGE_HOST, REGISTRY_HOST]


def test_the_cli_fails_without_writing_on_a_bad_allowlist(tmp_path: Path) -> None:
    bad = tmp_path / "allowlist.yaml"
    bad.write_text("egress:\n  provision: ['${MISSING}']\n  exec: []\n", encoding="utf-8")
    out = tmp_path / "squid.conf"
    argv = ["--allowlist", str(bad), "--provision-cidr", PROVISION_CIDR,
            "--exec-cidr", EXEC_CIDR, "--output", str(out)]  # fmt: skip
    assert rs.main(argv) == 2
    assert not out.exists()


# --- Terraform templates ----------------------------------------------------------------------

_INTERPOLATION = re.compile(r"(?<!\$)\$\{([a-z_]+)\}")


def _templatefile_keys(template: str) -> set[str]:
    """Keys of the map passed to templatefile("<...>/<template>", { ... }) in the module."""
    text = (MODULE / "main.tf").read_text(encoding="utf-8")
    start = text.index(f'/{template}", {{')
    body = text[start : text.index("})", start)]
    return set(re.findall(r"(?m)^\s*([a-z_]+)\s*=", body))


def _render_template(name: str, values: dict[str, str]) -> str:
    text = (CLOUD_INIT / name).read_text(encoding="utf-8")
    assert "%{" not in text, "templates use plain ${name} interpolation only"
    assert set(_INTERPOLATION.findall(text)) == set(values), name
    return _INTERPOLATION.sub(lambda m: values[m.group(1)], text)


def _nftables_conf() -> str:
    return _render_template(
        "nftables.conf.tftpl",
        {"provision_cidr": PROVISION_CIDR, "exec_cidr": EXEC_CIDR, "gateway_ip": GATEWAY_IP},
    )


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _user_data() -> str:
    def b64_file(name: str) -> str:
        return _b64((CLOUD_INIT / name).read_text(encoding="utf-8"))

    return _render_template(
        "egress-gateway.yaml.tftpl",
        {
            "provision_cidr": PROVISION_CIDR,
            "exec_cidr": EXEC_CIDR,
            "storage_host": STORAGE_HOST,
            "registry_host": REGISTRY_HOST,
            "nftables_conf": _b64(_nftables_conf()),
            "allowlist": _b64(ALLOWLIST.read_text(encoding="utf-8")),
            "render_squid": b64_file("render_squid.py"),
            "exec_pin": b64_file("p2p-exec-pin.sh"),
            "setup": b64_file("p2p-gateway-setup.sh"),
        },
    )


def _files(user_data: str) -> dict[str, str]:
    doc = yaml.safe_load(user_data)
    out = {}
    for entry in doc["write_files"]:
        content = entry["content"]
        if entry.get("encoding") == "b64":
            content = base64.b64decode(content).decode("utf-8")
        else:
            assert "encoding" not in entry, entry["path"]
        out[entry["path"]] = content
    return out


def test_the_module_passes_exactly_the_template_variables() -> None:
    for name in ("nftables.conf.tftpl", "egress-gateway.yaml.tftpl"):
        used = set(_INTERPOLATION.findall((CLOUD_INIT / name).read_text(encoding="utf-8")))
        assert _templatefile_keys(name) == used, name


def test_nftables_never_forwards_and_redirects_only_the_job_subnets() -> None:
    conf = _nftables_conf()
    assert "masquerade" not in conf and "snat" not in conf
    assert re.search(r"hook forward priority filter; policy drop;", conf)
    assert re.search(r"hook input priority filter; policy drop;", conf)
    redirects = [line.strip() for line in conf.splitlines() if "redirect to" in line]
    assert redirects == [
        "ip saddr $provision tcp dport 443 redirect to :3129",
        "ip saddr $provision tcp dport 80 redirect to :3128",
        "ip saddr $exec ip daddr @exec_dst4 tcp dport 443 redirect to :3129",
        "ip saddr $exec ip daddr @exec_dst4 tcp dport 80 redirect to :3128",
    ]
    assert f"define provision = {PROVISION_CIDR}" in conf
    assert f"define exec = {EXEC_CIDR}" in conf
    assert f"define gateway = {GATEWAY_IP}" in conf


def test_nftables_logs_and_drops_whatever_is_not_redirected() -> None:
    conf = _nftables_conf()
    deny = conf[conf.index("chain deny_unredirected") :]
    deny = [line.strip() for line in deny[: deny.index("\t}")].splitlines()]
    assert deny[2:] == [
        "ip saddr { $provision, $exec } ct status dnat accept",
        "ip saddr { $provision, $exec } ip daddr != $gateway ct state new limit rate 20/second"
        ' burst 100 packets log prefix "p2p-egress-deny " level info',
        "ip saddr { $provision, $exec } ip daddr != $gateway counter drop",
    ]
    # DHCP replies are accepted from the VPC only, never from a job subnet.
    assert "ip saddr != { $provision, $exec } udp sport 67 udp dport 68 accept" in conf
    # Squid's ports accept only redirected traffic, never a direct connection.
    assert "tcp dport { 3128, 3129 } ct status dnat accept" in conf


def test_cloud_init_is_a_cloud_config_without_logins() -> None:
    user_data = _user_data()
    assert user_data.startswith("#cloud-config\n")
    doc = yaml.safe_load(user_data)
    assert {"squid-openssl", "nftables", "python3-yaml", "openssl"} <= set(doc["packages"])
    assert doc["users"] == []
    assert doc["ssh_pwauth"] is False
    assert doc["disable_root"] is True
    assert "ssh_authorized_keys" not in user_data
    assert "PRIVATE KEY" not in user_data  # dummy.pem is generated on the VM
    assert doc["package_upgrade"] is True
    assert doc["runcmd"] == [["/usr/local/sbin/p2p-gateway-setup"]]


def test_cloud_init_ships_the_rendered_files() -> None:
    files = _files(_user_data())
    assert files["/etc/nftables.conf"] == _nftables_conf()
    assert files["/etc/p2p/allowlist.yaml"] == ALLOWLIST.read_text(encoding="utf-8")
    renderer = (CLOUD_INIT / "render_squid.py").read_text(encoding="utf-8")
    assert files["/usr/local/sbin/p2p-render-squid"] == renderer
    env = dict(line.split("=", 1) for line in files["/etc/p2p/gateway.env"].splitlines() if line)
    assert env == {
        "P2P_PROVISION_CIDR": PROVISION_CIDR,
        "P2P_EXEC_CIDR": EXEC_CIDR,
        "NEBIUS_STORAGE_HOST": STORAGE_HOST,
        "NEBIUS_REGISTRY_HOST": REGISTRY_HOST,
    }


def test_cloud_init_disables_ip_forwarding() -> None:
    sysctl = _files(_user_data())["/etc/sysctl.d/99-zz-p2p-egress-gateway.conf"]
    settings = dict(
        (k.strip(), v.strip()) for k, v in (line.split("=") for line in sysctl.splitlines() if line)
    )
    assert settings["net.ipv4.ip_forward"] == "0"
    assert settings["net.ipv6.conf.all.forwarding"] == "0"


def test_squid_starts_only_after_nftables() -> None:
    dropin = _files(_user_data())["/etc/systemd/system/squid.service.d/10-p2p.conf"]
    assert "Requires=nftables.service" in dropin
    # ... and after the first refresh of exec's destination set.
    assert "After=nftables.service p2p-exec-pin.service" in dropin
    assert "Wants=p2p-exec-pin.service" in dropin
    assert "NoNewPrivileges=yes" in dropin


def test_setup_stops_at_the_first_error_and_parses_before_starting_squid() -> None:
    script = (CLOUD_INIT / "p2p-gateway-setup.sh").read_text(encoding="utf-8")
    assert script.splitlines()[0] == "#!/bin/sh"
    assert "set -eu" in script.splitlines()[1:6]
    order = [
        "systemctl restart nftables.service",
        "p2p-render-squid",
        "squid -k parse -f /etc/squid/squid.conf",
        "systemctl restart squid.service",
    ]
    positions = [script.index(step) for step in order]
    assert positions == sorted(positions)
    # Missing packages are retried, and the forwarding switch is checked, not assumed.
    assert "apt-get install" in script and 'test "$(sysctl -n net.ipv4.ip_forward)" = 0' in script
    assert "trap " in script and "/dev/console" in script


def test_the_vm_is_created_stopped() -> None:
    main = _hcl(MODULE / "main.tf")
    assert re.search(r"stopped\s*=\s*var\.create_stopped", main)
    variables = _hcl(MODULE / "variables.tf")
    block = variables[variables.index('variable "create_stopped"') :]
    assert re.search(r"default\s*=\s*true", block[: block.index("}")])


def test_cloud_init_fits_the_user_data_limit() -> None:
    # Assumption (H-04): Nebius accepts at least 32 KiB of user data; the module enforces it too.
    assert len(_user_data().encode("utf-8")) <= 32768


# --- Terraform module (static) ----------------------------------------------------------------


def _hcl(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return re.sub(r"(?m)#.*$", "", text)


def test_security_rules_sit_between_100_and_200() -> None:
    main = _hcl(MODULE / "main.tf")
    priorities = [int(p) for p in re.findall(r"\bpriority\s*=\s*(\d+)", main)]
    assert priorities, "no security rule priorities found"
    assert all(100 <= p <= 200 for p in priorities), priorities


def test_security_rules_allow_only_web_and_icmp_from_the_job_subnets() -> None:
    main = _hcl(MODULE / "main.tf")
    rules = dict(re.findall(r'(?m)^\s*"?([a-z-]+)"?\s*=\s*\{\s*(direction\s*=.*)\}\s*$', main))
    assert set(rules) == {
        "allow-ingress-web", "allow-ingress-icmp", "deny-ingress-all",
        "allow-egress-web", "deny-egress-all",
    }  # fmt: skip
    for name, body in rules.items():
        if name.startswith("allow-"):
            assert re.search(r'access\s*=\s*"ALLOW"', body), name
            assert "ANY" not in body, name
        else:
            assert re.search(r'access\s*=\s*"DENY"', body), name
            assert re.search(r'protocol\s*=\s*"ANY"', body), name
    assert "[80, 443]" in rules["allow-ingress-web"]
    assert "local.routed_cidrs" in rules["allow-ingress-web"]
    assert '"ICMP"' in rules["allow-ingress-icmp"]
    assert "[80, 443]" in rules["allow-egress-web"]


def test_the_gateway_vm_has_no_identity_and_no_login() -> None:
    main = _hcl(MODULE / "main.tf")
    assert "service_account_id" not in main  # no IAM token on the gateway's metadata
    assert "ssh_authorized_keys" not in main


def test_the_gateway_nic_carries_the_allocation_a_public_ip_and_the_group() -> None:
    main = _hcl(MODULE / "main.tf")
    assert re.search(r"allocation_id\s*=\s*var\.private_allocation_id", main)
    assert re.search(r"public_ip_address\s*=\s*\{\s*\}", main)
    assert re.search(r"security_groups\s*=\s*\[\{\s*id\s*=\s*nebius_vpc_v1_security_group", main)
    assert re.search(r"ignore_changes\s*=\s*\[stopped\]", main)  # on-demand start/stop


def test_envs_prod_wires_the_gateway_to_the_allowlist() -> None:
    prod = _hcl(PROD / "main.tf")
    assert 'module "egress_gateway"' in prod
    assert re.search(r'allowlist_yaml\s*=\s*file\(".*config/allowlist\.yaml"\)', prod)
    assert "module.network_sandbox.gateway_allocation_id" in prod
    assert "GATEWAY_INSTANCE_ID" in _hcl(PROD / "outputs.tf")


def test_allowlist_relative_path_resolves_from_envs_prod() -> None:
    prod = (PROD / "main.tf").read_text(encoding="utf-8")
    match = re.search(r'file\("\$\{path\.root\}/([^"]+allowlist\.yaml)"\)', prod)
    assert match is not None
    assert (PROD / match.group(1)).resolve() == ALLOWLIST.resolve()


# --- Real tools in a container (pytest -m docker) ---------------------------------------------


def _docker(args: list[str], timeout: float = 900) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - the docker CLI with arguments built by these tests
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def gateway_image() -> str:
    try:
        built = _docker(
            ["build", "-t", TEST_IMAGE, "-f", str(ROOT / "tests/infra/Dockerfile.gateway-test"),
             str(ROOT / "tests/infra")]
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"docker not available: {exc}")
    assert built.returncode == 0, built.stderr[-3000:]
    return TEST_IMAGE


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """Inputs for the container, mounted read-only at /cfg."""
    (tmp_path / "squid.conf").write_text(_render(), encoding="utf-8", newline="\n")
    (tmp_path / "nftables.conf").write_text(_nftables_conf(), encoding="utf-8", newline="\n")
    (tmp_path / "user-data").write_text(_user_data(), encoding="utf-8", newline="\n")
    for name in ("render_squid.py", "p2p-exec-pin.sh", "p2p-gateway-setup.sh"):
        text = (CLOUD_INIT / name).read_text(encoding="utf-8")
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
    (tmp_path / "allowlist.yaml").write_text(
        ALLOWLIST.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    return tmp_path


def _in_container(image: str, staged: Path, script: str, *extra: str, network: str = "none") -> str:
    done = _docker(
        ["run", "--rm", "--network", network, *extra, "-v", f"{staged}:/cfg:ro", image,
         "sh", "-euc", script]
    )  # fmt: skip
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    return done.stdout + done.stderr


DUMMY_CERT = (
    "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1"
    " -subj /CN=p2p-egress-gateway -keyout /etc/squid/dummy.pem -out /etc/squid/dummy.pem"
    " 2>/dev/null"
)


@pytest.mark.docker
def test_squid_parses_the_rendered_config(gateway_image: str, staged: Path) -> None:
    out = _in_container(gateway_image, staged, f"{DUMMY_CERT}; squid -k parse -f /cfg/squid.conf")
    assert "FATAL" not in out and "ERROR" not in out, out


@pytest.mark.docker
def test_squid_is_built_with_openssl(gateway_image: str, staged: Path) -> None:
    assert "--with-openssl" in _in_container(gateway_image, staged, "squid -v")


@pytest.mark.docker
def test_the_gateway_python_renders_the_same_config(gateway_image: str, staged: Path) -> None:
    script = (
        "python3 /cfg/render_squid.py --allowlist /cfg/allowlist.yaml"
        f" --provision-cidr {PROVISION_CIDR} --exec-cidr {EXEC_CIDR}"
        f" --var NEBIUS_STORAGE_HOST={STORAGE_HOST} --var NEBIUS_REGISTRY_HOST={REGISTRY_HOST}"
        " --output /tmp/squid.conf && cmp /tmp/squid.conf /cfg/squid.conf"
    )
    _in_container(gateway_image, staged, script)


@pytest.mark.docker
def test_nft_checks_the_rendered_ruleset(gateway_image: str, staged: Path) -> None:
    _in_container(gateway_image, staged, "nft -c -f /cfg/nftables.conf", "--cap-add", "NET_ADMIN")


@pytest.mark.docker
def test_exec_pinning_fills_the_destination_set(gateway_image: str, staged: Path) -> None:
    # Two hosts share an address, a third does not resolve; a second run refreshes the timeouts.
    script = (
        "echo '192.0.2.7 pin-a.test pin-b.test' >> /etc/hosts"
        " && echo '192.0.2.8 pin-c.test' >> /etc/hosts"
        " && printf 'pin-a.test\\npin-b.test\\nmissing.invalid\\npin-c.test\\n' > /tmp/hosts"
        " && nft -f /cfg/nftables.conf"
        " && sh /cfg/p2p-exec-pin.sh /tmp/hosts && sh /cfg/p2p-exec-pin.sh /tmp/hosts"
        " && nft list set inet p2p exec_dst4"
    )
    # getent asks for AI_ADDRCONFIG: it needs a non-loopback IPv4 address (no traffic is sent).
    out = _in_container(gateway_image, staged, script, "--cap-add", "NET_ADMIN", network="bridge")
    elements = re.search(r"elements = \{([^}]*)\}", out)
    assert elements is not None, out
    entries = [e.split()[0] for e in elements.group(1).split(",")]
    assert sorted(ipaddress.ip_address(e) for e in entries) == [
        ipaddress.ip_address("192.0.2.7"),
        ipaddress.ip_address("192.0.2.8"),
    ]
    assert "missing.invalid did not resolve" in out
    assert elements.group(1).count("expires") == 2  # every element carries a timeout


@pytest.mark.docker
def test_exec_pinning_keeps_the_set_when_nothing_resolves(gateway_image: str, staged: Path) -> None:
    script = (
        "nft -f /cfg/nftables.conf"
        " && nft add element inet p2p exec_dst4 '{ 192.0.2.9 timeout 15m }'"
        " && printf 'missing.invalid\\n' > /tmp/hosts && sh /cfg/p2p-exec-pin.sh /tmp/hosts"
        " && nft list set inet p2p exec_dst4"
    )
    out = _in_container(gateway_image, staged, script, "--cap-add", "NET_ADMIN", network="bridge")
    assert "192.0.2.9" in out and "left as it is" in out


@pytest.mark.docker
def test_cloud_init_schema_accepts_the_user_data(gateway_image: str, staged: Path) -> None:
    out = _in_container(gateway_image, staged, "cloud-init schema --config-file /cfg/user-data")
    assert "Valid schema" in out, out


@pytest.mark.docker
def test_the_shell_scripts_are_valid_posix_sh(gateway_image: str, staged: Path) -> None:
    _in_container(gateway_image, staged, 'for f in /cfg/*.sh; do dash -n "$f"; done')
