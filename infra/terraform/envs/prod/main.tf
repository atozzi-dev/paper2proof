# Paper2Proof production infrastructure (architecture §12). Projects are created by hand:
# p2p-control (endpoint, state, bundles, secrets) and p2p-sandbox (jobs, per-run buckets, cache,
# egress gateway) in H-01. The bundle signing key lives in p2p-signing and is managed by
# envs/prod-iam (H-04). Serverless endpoints and jobs are not Terraform resources: the endpoint is managed by
# deploy/endpoint.sh, jobs and per-run buckets by the orchestrator.
#
# Credentials come only from the environment: NEBIUS_IAM_TOKEN for the provider (a human's
# `nebius iam get-access-token` at bootstrap, the OIDC-exchanged token in CI) and AWS_* for the
# state backend. Nothing secret is written to variables, outputs or state (invariant 4).

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = "= 0.6.64"
    }
  }
}

locals {
  # Regional Object Storage endpoint (NEBIUS_STORAGE_HOST), allowlisted on the egress gateway.
  storage_host = "storage.${var.region}.nebius.cloud"
}

provider "nebius" {
  default_labels = {
    app        = "paper2proof"
    managed-by = "terraform"
  }
}

module "network_control" {
  source = "../../modules/network_control"

  project_id = var.control_project_id
}

module "network_sandbox" {
  source = "../../modules/network_sandbox"

  project_id     = var.sandbox_project_id
  provision_cidr = var.provision_cidr
  exec_cidr      = var.exec_cidr
  gateway_cidr   = var.gateway_cidr
}

# The only egress of provision and exec (A-04). It carries the allocation their routes point at.
# Not created in the hybrid deploy (enable_gateway = false, docs/specs/hybrid-deploy.md): those
# routes then have no next hop, so jobs in provision and exec have no internet at all.
module "egress_gateway" {
  source = "../../modules/egress_gateway"
  count  = var.enable_gateway ? 1 : 0

  project_id            = var.sandbox_project_id
  network_id            = module.network_sandbox.network_id
  subnet_id             = module.network_sandbox.subnet_ids.gateway
  private_allocation_id = module.network_sandbox.gateway_allocation_id
  private_ip            = module.network_sandbox.gateway_private_ip
  provision_cidr        = module.network_sandbox.subnet_cidrs.provision
  exec_cidr             = module.network_sandbox.subnet_cidrs.exec
  storage_host          = local.storage_host
  registry_host         = module.registry_sandbox.host
  allowlist_yaml        = file("${path.root}/../../../../config/allowlist.yaml")
}

moved {
  from = module.egress_gateway
  to   = module.egress_gateway[0]
}

module "storage" {
  source = "../../modules/storage"

  control_project_id  = var.control_project_id
  sandbox_project_id  = var.sandbox_project_id
  state_bucket_name   = var.state_bucket_name
  bundles_bucket_name = var.bundles_bucket_name
  cache_bucket_name   = var.cache_bucket_name
}

module "registry_control" {
  source = "../../modules/registry"

  project_id  = var.control_project_id
  name        = "p2p-control"
  description = "Control-plane image (api)."
}

module "registry_sandbox" {
  source = "../../modules/registry"

  project_id  = var.sandbox_project_id
  name        = "p2p-sandbox"
  description = "Job images (runner, runner-cpu, provisioner)."
}

module "secrets" {
  source = "../../modules/secrets"

  project_id = var.control_project_id
}

module "postgres" {
  source = "../../modules/postgres"

  enabled    = var.enable_managed_pg
  project_id = var.control_project_id
  network_id = module.network_control.network_id
  password   = var.pg_password
}
