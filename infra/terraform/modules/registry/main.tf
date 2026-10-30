# One Container Registry per project (architecture §12.1): p2p-control holds the api image,
# p2p-sandbox the runner, runner-cpu and provisioner images. Images are always referenced by
# digest (invariant 8); tags are only a convenience for humans.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.64, < 0.7.0"
    }
  }
}

resource "nebius_registry_v1_registry" "this" {
  parent_id   = var.project_id
  name        = var.name
  description = var.description
}
