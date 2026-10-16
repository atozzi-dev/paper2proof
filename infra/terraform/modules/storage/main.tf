# Static buckets (architecture §10.3). Per-run buckets p2p-run-<slug> are created and deleted at
# runtime by the orchestrator (get-by-name first, invariant 9) and are not managed here.
# p2p-tfstate is created by hand before the first apply (bootstrap), so it is not here either.
# No bucket policies: managing them needs `admin`, and this module is applied by CI as `editor`.
# sa-api-runtime reaches p2p-state and p2p-bundles through bucket access permits (envs/prod-iam).

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
  # Abort leftover multipart uploads after a day on every bucket.
  abort_multipart = {
    id                                = "abort-incomplete-multipart"
    status                            = "ENABLED"
    abort_incomplete_multipart_upload = { days_after_initiation = 1 }
  }

  # On versioned buckets, drop delete markers once no noncurrent version is left behind them.
  drop_delete_markers = {
    id         = "drop-expired-delete-markers"
    status     = "ENABLED"
    expiration = { expired_object_delete_marker = true }
  }
}

# Litestream replica of the control-plane SQLite database.
resource "nebius_storage_v1_bucket" "state" {
  parent_id            = var.control_project_id
  name                 = var.state_bucket_name
  versioning_policy    = "ENABLED"
  object_audit_logging = "MUTATE_ONLY"

  lifecycle_configuration = {
    rules = [
      local.abort_multipart,
      local.drop_delete_markers,
      {
        id                            = "expire-noncurrent-7d"
        status                        = "ENABLED"
        noncurrent_version_expiration = { noncurrent_days = 7 }
      },
    ]
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Proof Bundles (bundles/<run_id>/...). Versioning is their backup; kept until the end of the
# judging window plus margin, then expired.
resource "nebius_storage_v1_bucket" "bundles" {
  parent_id            = var.control_project_id
  name                 = var.bundles_bucket_name
  versioning_policy    = "ENABLED"
  object_audit_logging = "MUTATE_ONLY"

  lifecycle_configuration = {
    rules = [
      local.abort_multipart,
      local.drop_delete_markers,
      {
        id         = "expire-after-judging"
        status     = "ENABLED"
        expiration = { date = var.bundles_expire_on }
      },
      # Without this, expiry only adds delete markers and the versions stay forever.
      {
        id                            = "expire-noncurrent-30d"
        status                        = "ENABLED"
        noncurrent_version_expiration = { noncurrent_days = 30 }
      },
    ]
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Shared wheels and datasets, deduplicated by sha256, mounted read-only in EXECUTE jobs.
resource "nebius_storage_v1_bucket" "cache" {
  parent_id         = var.sandbox_project_id
  name              = var.cache_bucket_name
  versioning_policy = "DISABLED"

  lifecycle_configuration = {
    rules = [
      local.abort_multipart,
      {
        id         = "expire-${var.cache_expiry_days}d"
        status     = "ENABLED"
        expiration = { days = var.cache_expiry_days }
      },
    ]
  }
}
