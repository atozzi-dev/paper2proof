# Egress gateway of net-sandbox (architecture §8.2, A-04): the only way out of the provision and
# exec subnets, whose 0.0.0.0/0 routes point at the private allocation this VM carries.
#
#   - nftables redirects tcp/443 and tcp/80 from provision and exec to a local Squid in intercept
#     mode, which peeks at the TLS SNI and splices it only when it is on the source subnet's
#     allowlist (config/allowlist.yaml), terminating everything else. Nothing is decrypted.
#   - The VM never forwards packets and has no NAT rule: what Squid does not carry is dropped and
#     logged. If the VM is stopped or broken, jobs have no egress at all (fail-closed).
#   - The VM has no service account, no SSH key and no login user.
#
# The orchestrator starts and stops the VM on demand (architecture §17), so Terraform ignores the
# `stopped` attribute after creation, and finds the VM by name (its ID changes on replacement). Any change to the cloud-init inputs replaces the VM: cloud-init runs once.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.64, < 0.7.0"
    }
  }
}

locals {
  cloud_init_dir = "${path.module}/../../../cloud-init"
  routed_cidrs   = [var.provision_cidr, var.exec_cidr]

  nftables_conf = templatefile("${local.cloud_init_dir}/nftables.conf.tftpl", {
    provision_cidr = var.provision_cidr
    exec_cidr      = var.exec_cidr
    gateway_ip     = var.private_ip
  })

  user_data = templatefile("${local.cloud_init_dir}/egress-gateway.yaml.tftpl", {
    provision_cidr = var.provision_cidr
    exec_cidr      = var.exec_cidr
    storage_host   = var.storage_host
    registry_host  = var.registry_host
    nftables_conf  = base64encode(local.nftables_conf)
    allowlist      = base64encode(var.allowlist_yaml)
    render_squid   = filebase64("${local.cloud_init_dir}/render_squid.py")
    exec_pin       = filebase64("${local.cloud_init_dir}/p2p-exec-pin.sh")
    setup          = filebase64("${local.cloud_init_dir}/p2p-gateway-setup.sh")
  })

  # sg-egress-gw. The docs say every VM is also in the network's default group (allow all,
  # priority 500, stateless); the provider says the default group applies only when no other is
  # set. Either way rules are merged by priority and the first match wins, so the deny-all
  # rules at 200 shadow any allow at 500. Spike S-10 records which case holds (port scan from
  # exec). Traffic to and from 169.254.0.0/16 (DNS, metadata) is never evaluated.
  # ICMP is accepted from anywhere so path-MTU discovery works for Squid's downloads; nftables
  # answers echo requests from the job subnets only.
  security_rules = {
    allow-ingress-web  = { direction = "ingress", access = "ALLOW", protocol = "TCP", priority = 100, ports = [80, 443], cidrs = local.routed_cidrs }
    allow-ingress-icmp = { direction = "ingress", access = "ALLOW", protocol = "ICMP", priority = 110, ports = [], cidrs = [] }
    deny-ingress-all   = { direction = "ingress", access = "DENY", protocol = "ANY", priority = 200, ports = [], cidrs = [] }
    allow-egress-web   = { direction = "egress", access = "ALLOW", protocol = "TCP", priority = 100, ports = [80, 443], cidrs = [] }
    deny-egress-all    = { direction = "egress", access = "DENY", protocol = "ANY", priority = 200, ports = [], cidrs = [] }
  }
}

resource "nebius_vpc_v1_security_group" "gateway" {
  parent_id  = var.project_id
  name       = "sg-egress-gw"
  network_id = var.network_id
}

resource "nebius_vpc_v1_security_rule" "gateway" {
  for_each = local.security_rules

  parent_id = nebius_vpc_v1_security_group.gateway.id
  name      = each.key
  access    = each.value.access
  protocol  = each.value.protocol
  priority  = each.value.priority
  type      = "STATEFUL"

  # Empty lists mean "any" for the API; send null so the plan matches what it stores.
  ingress = each.value.direction == "ingress" ? {
    source_cidrs      = length(each.value.cidrs) > 0 ? each.value.cidrs : null
    destination_ports = length(each.value.ports) > 0 ? each.value.ports : null
  } : null

  egress = each.value.direction == "egress" ? {
    destination_cidrs = length(each.value.cidrs) > 0 ? each.value.cidrs : null
    destination_ports = length(each.value.ports) > 0 ? each.value.ports : null
  } : null
}

# Hash of everything cloud-init receives: a new value replaces the VM (cloud-init runs once).
resource "terraform_data" "cloud_init" {
  input = sha256(local.user_data)
}

resource "nebius_compute_v1_instance" "gateway" {
  parent_id = var.project_id
  name      = var.name
  hostname  = var.name

  # journald (Squid's access log, nftables denials, setup) -> Nebius Observability Logs, through
  # the monitoring agent of the Nebius image.
  labels = {
    "nebius.o11y.systemd-logs-collection.enabled" = "true"
  }

  resources = {
    platform = var.platform
    preset   = var.preset
  }

  boot_disk = {
    attach_mode = "READ_WRITE"
    device_id   = "boot"
    managed_disk = {
      name = "${var.name}-boot"
      spec = {
        type           = var.disk_type
        size_gibibytes = var.disk_size_gibibytes
        source_image_family = {
          image_family = var.image_family
        }
      }
    }
  }

  network_interfaces = [{
    name      = "eth0"
    subnet_id = var.subnet_id
    # The fixed private IP the default routes of provision and exec point at.
    ip_address = {
      allocation_id = var.private_allocation_id
    }
    # Dynamic public IP: Squid's upstream connections leave from it.
    public_ip_address = {}
    security_groups   = [{ id = nebius_vpc_v1_security_group.gateway.id }]
  }]

  cloud_init_user_data = local.user_data
  # Created stopped (cost: a replacement must not run unattended); the first start, by a human
  # at H-04 or by the orchestrator, runs cloud-init.
  stopped         = var.create_stopped
  recovery_policy = "RECOVER"

  lifecycle {
    ignore_changes       = [stopped]
    replace_triggered_by = [terraform_data.cloud_init]

    precondition {
      # Assumption to check at H-04: Nebius accepts at least 32 KiB of user data.
      condition     = length(local.user_data) <= 32768
      error_message = "The gateway's cloud-init user data exceeds 32 KiB."
    }
  }

  # The security rules must be in place before the VM boots and starts taking traffic.
  depends_on = [nebius_vpc_v1_security_rule.gateway]
}
