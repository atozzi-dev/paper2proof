# Identity and access for Paper2Proof (architecture §9): service accounts, groups, access permits,
# GitHub OIDC federated credentials, S3 access keys, plus the bundle signing key in p2p-signing.
# Everything here needs `admin` (or a project CI only views), so this root is applied only by a
# human (H-04, and on every IAM change), always from a reviewed `terraform plan -out`.
# Apply envs/prod first: the bucket and secret permits look those resources up by name.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = "= 0.6.64"
    }
  }
}

provider "nebius" {
  default_labels = {
    app        = "paper2proof"
    managed-by = "terraform"
  }
}

data "nebius_storage_v1_bucket" "runtime" {
  for_each = {
    state   = var.state_bucket_name
    bundles = var.bundles_bucket_name
  }

  parent_id = var.control_project_id
  name      = each.value
}

data "nebius_mysterybox_v1_secret" "app" {
  parent_id = var.control_project_id
  name      = var.secrets_name
}

module "project_iam" {
  source = "../../modules/project_iam"

  tenant_id              = var.tenant_id
  control_project_id     = var.control_project_id
  sandbox_project_id     = var.sandbox_project_id
  signing_project_id     = var.signing_project_id
  api_runtime_bucket_ids = { for k, b in data.nebius_storage_v1_bucket.runtime : k => b.id }
  secrets_id             = data.nebius_mysterybox_v1_secret.app.id
  enable_github_oidc     = var.enable_github_oidc
  github_repo            = var.github_repo
  github_environment     = var.github_environment
  github_owner_id        = var.github_owner_id
  github_repo_id         = var.github_repo_id
  github_oidc_subject    = var.github_oidc_subject
}

# In the human-only root: CI is only `viewer` on p2p-signing, and changing the key is a human act.
module "kms" {
  source = "../../modules/kms"

  project_id = var.signing_project_id
}
