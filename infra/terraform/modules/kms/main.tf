# Asymmetric signing key for Proof Bundles (A-26). The private half never leaves KMS; the
# control plane signs the sha256 of MANIFEST.json and the public key is published in the repo.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.64, < 0.7.0"
    }
  }
}

resource "nebius_kms_v1_asymmetric_key" "signing" {
  parent_id   = var.project_id
  name        = var.name
  description = "Signs the sha256 of each Proof Bundle MANIFEST.json."
  algorithm   = var.algorithm

  # Losing the key would leave published bundles unverifiable against a live key.
  lifecycle {
    prevent_destroy = true
  }
}
