# Service accounts, groups, access permits, GitHub OIDC federated credentials and S3 access keys
# (architecture §9.1, §9.2). Nebius general roles are granted per project (or per resource for
# buckets, registries, secrets and IAM objects), so least privilege comes from project separation:
#   p2p-control  endpoint, state, bundles, secrets          (no service account is editor here
#                                                            except the deployer)
#   p2p-sandbox  jobs, per-run buckets, cache, gateway        sa-job-launcher, in a group created
#                                                            inside the project, so it can never
#                                                            hold a permit anywhere else
#   p2p-signing  only the bundle signing key                 sa-api-runtime is editor here because
#                                                            KMS signing needs it and KMS keys take
#                                                            no resource-level permits
#
# Applied only by a human with tenant-admin credentials, from envs/prod-iam: groups, access permits
# and access keys need the `admin` role, which no service account here has.

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
  github_oidc_subject = coalesce(
    var.github_oidc_subject,
    "repo:${var.github_repo}:environment:${var.github_environment}:ref:${var.github_ref}",
  )
}

# --- Service accounts -------------------------------------------------------------------------

resource "nebius_iam_v1_service_account" "ci_deployer" {
  parent_id   = var.control_project_id
  name        = "sa-ci-deployer"
  description = "GitHub Actions deploys: image push, endpoint update, terraform apply (non-IAM)."
}

resource "nebius_iam_v1_service_account" "api_runtime" {
  parent_id   = var.control_project_id
  name        = "sa-api-runtime"
  description = "Control-plane endpoint: p2p-state and p2p-bundles objects, bundle signing, secret reads."
}

resource "nebius_iam_v1_service_account" "job_launcher" {
  parent_id   = var.sandbox_project_id
  name        = "sa-job-launcher"
  description = "Orchestrator in p2p-sandbox only: jobs, per-run buckets, gateway start/stop."
}

# --- Groups and memberships -------------------------------------------------------------------

# Tenant-level groups for the two accounts that act in more than one project. Their permits name
# each project or resource explicitly; none is granted on the tenant.
resource "nebius_iam_v1_group" "ci_deployer" {
  parent_id = var.tenant_id
  name      = "p2p-ci-deployer"
}

resource "nebius_iam_v1_group" "api_runtime" {
  parent_id = var.tenant_id
  name      = "p2p-api-runtime"
}

# Project-level group: it can only ever receive permits inside p2p-sandbox.
resource "nebius_iam_v1_group" "job_launcher" {
  parent_id = var.sandbox_project_id
  name      = "p2p-job-launcher"
}

resource "nebius_iam_v1_group_membership" "ci_deployer" {
  parent_id = nebius_iam_v1_group.ci_deployer.id
  member_id = nebius_iam_v1_service_account.ci_deployer.id
}

resource "nebius_iam_v1_group_membership" "api_runtime" {
  parent_id = nebius_iam_v1_group.api_runtime.id
  member_id = nebius_iam_v1_service_account.api_runtime.id
}

resource "nebius_iam_v1_group_membership" "job_launcher" {
  parent_id = nebius_iam_v1_group.job_launcher.id
  member_id = nebius_iam_v1_service_account.job_launcher.id
}

# --- Access permits ---------------------------------------------------------------------------

# `editor` and not `admin`: the deployer cannot change IAM (groups, permits, keys), so a stolen
# CI token cannot escalate. Only `viewer` on p2p-signing: the key is created and changed by a human
# apply, and CI cannot sign bundles.
resource "nebius_iam_v1_access_permit" "ci_deployer" {
  for_each = {
    control = { project = var.control_project_id, role = "editor" }
    sandbox = { project = var.sandbox_project_id, role = "editor" }
    signing = { project = var.signing_project_id, role = "viewer" }
  }

  parent_id   = nebius_iam_v1_group.ci_deployer.id
  resource_id = each.value.project
  role        = each.value.role
}

# The runtime has no project-wide role in p2p-control: objects in its two buckets and the payload
# of p2p-secrets only, plus `editor` on the project that holds nothing but the signing key.
resource "nebius_iam_v1_access_permit" "api_runtime_signing" {
  parent_id   = nebius_iam_v1_group.api_runtime.id
  resource_id = var.signing_project_id
  role        = "editor"
}

resource "nebius_iam_v1_access_permit" "api_runtime_buckets" {
  for_each = var.api_runtime_bucket_ids

  parent_id   = nebius_iam_v1_group.api_runtime.id
  resource_id = each.value
  role        = "storage.object-editor"
}

# `editor` cannot read payloads; mysterybox.payload-viewer is granted on this one secret only.
resource "nebius_iam_v1_access_permit" "api_runtime_secrets" {
  parent_id   = nebius_iam_v1_group.api_runtime.id
  resource_id = var.secrets_id
  role        = "mysterybox.payload-viewer"
}

resource "nebius_iam_v1_access_permit" "job_launcher" {
  parent_id   = nebius_iam_v1_group.job_launcher.id
  resource_id = var.sandbox_project_id
  role        = "editor"
}

# --- GitHub Actions OIDC (spike S-7) ----------------------------------------------------------

# Only jobs of this repository that run in the protected `prod` environment AND on main can
# impersonate sa-ci-deployer. GitHub's default `sub` for an environment job has no ref, so the
# repository's OIDC subject template must include it (H-04: include_claim_keys repo, context, ref).
resource "nebius_iam_v1_federated_credentials" "github" {
  count = var.enable_github_oidc ? 1 : 0

  parent_id            = var.control_project_id
  name                 = "gha-${replace(var.github_repo, "/", "-")}-${var.github_environment}"
  subject_id           = nebius_iam_v1_service_account.ci_deployer.id
  federated_subject_id = local.github_oidc_subject

  oidc_provider = {
    issuer_url = var.github_oidc_issuer
  }
}

# --- S3 access keys ---------------------------------------------------------------------------

# MYSTERY_BOX delivery: the secret half goes straight into a SecretStash secret and never appears
# in the API response, so it is not in the Terraform state either (invariant 4). Only the key ID
# and the reference to that secret are exported. Bucket access comes from the bucket permits above
# and the job launcher's project role, never from a role on p2p-control.
resource "nebius_iam_v2_access_key" "api_runtime" {
  parent_id            = var.control_project_id
  name                 = "ak-api-runtime"
  description          = "S3 key for Litestream (p2p-state) and Proof Bundles (p2p-bundles)."
  secret_delivery_mode = "MYSTERY_BOX"

  account = {
    service_account = {
      id = nebius_iam_v1_service_account.api_runtime.id
    }
  }
}

resource "nebius_iam_v2_access_key" "job_launcher" {
  parent_id            = var.sandbox_project_id
  name                 = "ak-job-launcher"
  description          = "S3 key to stage inputs and read results in per-run buckets."
  secret_delivery_mode = "MYSTERY_BOX"

  account = {
    service_account = {
      id = nebius_iam_v1_service_account.job_launcher.id
    }
  }
}
