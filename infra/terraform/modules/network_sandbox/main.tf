# net-sandbox (architecture §8.1): where untrusted code runs. Three subnets:
#   provision  PROVISION jobs (allowlisted egress through the gateway)
#   exec       EXECUTE GPU jobs (Object Storage and Container Registry only, through the gateway)
#   gateway    the egress gateway VM (A-04). It keeps the network's default route table: a custom
#              table on it would route the gateway's own traffic back to itself (routing loop).
#
# provision and exec send 0.0.0.0/0 to the gateway's private allocation and have no public IP
# pool, so a job cannot get a floating IP whose route would bypass the gateway. Fail-closed: until
# A-04 attaches the allocation to the gateway VM (or while the VM is stopped) the next hop is a
# black hole and jobs have no internet at all.

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
  gateway_private_ip = cidrhost(var.gateway_cidr, var.gateway_host_index)

  # Subnets whose default route goes through the gateway.
  routed_subnets = {
    provision = var.provision_cidr
    exec      = var.exec_cidr
  }
}

resource "nebius_vpc_v1_network" "sandbox" {
  parent_id = var.project_id
  name      = var.network_name
}

resource "nebius_vpc_v1_subnet" "gateway" {
  parent_id  = var.project_id
  name       = "gateway"
  network_id = nebius_vpc_v1_network.sandbox.id

  ipv4_private_pools = {
    use_network_pools = false
    pools             = [{ cidrs = [{ cidr = var.gateway_cidr }] }]
  }
  # Public pools stay on: the gateway VM needs a public IP to reach the internet.
}

# Private /32 the routes point at. A-04 attaches it to the gateway VM's network interface.
resource "nebius_vpc_v1_allocation" "gateway" {
  parent_id = var.project_id
  name      = "gateway-private"

  ipv4_private = {
    subnet_id = nebius_vpc_v1_subnet.gateway.id
    cidr      = local.gateway_private_ip
  }

  # Replacing it would detach the next hop from the gateway VM (an egress outage for every job):
  # make that an explicit, human decision.
  lifecycle {
    prevent_destroy = true
  }
}

resource "nebius_vpc_v1_route_table" "routed" {
  for_each = local.routed_subnets

  parent_id  = var.project_id
  name       = "rt-${each.key}"
  network_id = nebius_vpc_v1_network.sandbox.id
}

resource "nebius_vpc_v1_route" "default_via_gateway" {
  for_each = local.routed_subnets

  parent_id   = nebius_vpc_v1_route_table.routed[each.key].id
  name        = "default-via-gateway"
  description = "All egress of ${each.key} goes through the egress gateway."

  destination = {
    cidr = "0.0.0.0/0"
  }

  next_hop = {
    allocation = {
      id = nebius_vpc_v1_allocation.gateway.id
    }
  }
}

resource "nebius_vpc_v1_subnet" "routed" {
  for_each = local.routed_subnets

  parent_id      = var.project_id
  name           = each.key
  network_id     = nebius_vpc_v1_network.sandbox.id
  route_table_id = nebius_vpc_v1_route_table.routed[each.key].id

  ipv4_private_pools = {
    use_network_pools = false
    pools             = [{ cidrs = [{ cidr = each.value }] }]
  }

  ipv4_public_pools = {
    use_network_pools = false
  }

  # The route must exist before a job can land in the subnet.
  depends_on = [nebius_vpc_v1_route.default_via_gateway]
}
