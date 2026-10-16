# net-control (architecture §8.1): the control plane's own network. It has no peering with
# net-sandbox, so code running in a job can never reach the control plane by private IP.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.64, < 0.7.0"
    }
  }
}

resource "nebius_vpc_v1_network" "control" {
  parent_id = var.project_id
  name      = var.network_name
}

# Uses the network's private pools and default route table (internet egress for the endpoint).
resource "nebius_vpc_v1_subnet" "control" {
  parent_id  = var.project_id
  name       = var.subnet_name
  network_id = nebius_vpc_v1_network.control.id
}
