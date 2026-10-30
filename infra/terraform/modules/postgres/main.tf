# Managed PostgreSQL, off by default (architecture §10.1, ADR-002): SQLite + Litestream is the
# state store. Turning it on is the migration path (new DSN + `alembic upgrade`).

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.64, < 0.7.0"
    }
  }
}

resource "nebius_msp_postgresql_v1alpha1_cluster" "this" {
  count = var.enabled ? 1 : 0

  parent_id  = var.project_id
  name       = var.name
  network_id = var.network_id

  bootstrap = {
    db_name   = var.db_name
    user_name = var.user_name
  }

  # Write-only: the password comes from an ephemeral variable and is never stored in the state.
  sensitive = {
    version = var.password_version
    bootstrap = {
      user_password = var.password
    }
  }

  config = {
    version       = "16"
    public_access = false
    template = {
      hosts     = { count = 1 }
      resources = { platform = var.platform, preset = var.preset }
      disk      = { type = var.disk_type, size_gibibytes = var.disk_size_gibibytes }
    }
  }

  backup = {
    retention_policy = "7d"
  }
}
