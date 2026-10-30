variable "region" {
  description = "Nebius region of both projects."
  type        = string
  default     = "eu-north1"

  validation {
    condition     = var.region == "eu-north1"
    error_message = "Paper2Proof runs in eu-north1 only (backend.tf and the GPU quota assume it)."
  }
}

variable "control_project_id" {
  description = "ID of the p2p-control project (NEBIUS_CONTROL_PROJECT_ID)."
  type        = string
}

variable "sandbox_project_id" {
  description = "ID of the p2p-sandbox project (NEBIUS_SANDBOX_PROJECT_ID)."
  type        = string
}

variable "provision_cidr" {
  description = "CIDR of the provision subnet in net-sandbox."
  type        = string
  default     = "10.1.0.0/24"
}

variable "exec_cidr" {
  description = "CIDR of the exec subnet in net-sandbox."
  type        = string
  default     = "10.1.1.0/24"
}

variable "gateway_cidr" {
  description = "CIDR of the gateway subnet in net-sandbox."
  type        = string
  default     = "10.1.2.0/27"
}

variable "state_bucket_name" {
  description = "Name of the Litestream bucket."
  type        = string
  default     = "p2p-state"
}

variable "bundles_bucket_name" {
  description = "Name of the Proof Bundle bucket."
  type        = string
  default     = "p2p-bundles"
}

variable "cache_bucket_name" {
  description = "Name of the shared cache bucket."
  type        = string
  default     = "p2p-cache"
}

variable "enable_gateway" {
  description = "Create the egress gateway VM (A-04). false for the hybrid deploy (no non-GPU vCPU quota, docs/specs/hybrid-deploy.md): provision and exec then have no egress at all."
  type        = bool
  default     = true
}

variable "enable_managed_pg" {
  description = "Create the Managed PostgreSQL cluster (migration path off SQLite)."
  type        = bool
  default     = false
}

variable "pg_password" {
  description = "PostgreSQL bootstrap password, only when enable_managed_pg is true. Pass it as TF_VAR_pg_password; it is write-only and never stored."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = !var.enable_managed_pg || var.pg_password != null
    error_message = "pg_password (TF_VAR_pg_password) is required when enable_managed_pg is true."
  }
}
