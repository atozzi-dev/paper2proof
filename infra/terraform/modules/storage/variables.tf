variable "control_project_id" {
  description = "ID of the p2p-control project (p2p-state, p2p-bundles)."
  type        = string
}

variable "sandbox_project_id" {
  description = "ID of the p2p-sandbox project (p2p-cache)."
  type        = string
}

# Bucket names may have to be globally unique (spike S-11): override them in tfvars if taken.
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
  description = "Name of the shared wheel and dataset cache bucket."
  type        = string
  default     = "p2p-cache"
}

variable "bundles_expire_on" {
  description = "Date (RFC 3339, midnight UTC) after which Proof Bundles expire."
  type        = string
  default     = "2027-01-31T00:00:00Z"

  validation {
    condition     = can(regex("^\\d{4}-\\d{2}-\\d{2}T00:00:00Z$", var.bundles_expire_on))
    error_message = "bundles_expire_on must be an RFC 3339 date at midnight UTC."
  }
}

variable "cache_expiry_days" {
  description = "Days after upload before a cached wheel or dataset expires (architecture §10.3: 30)."
  type        = number
  default     = 30

  validation {
    condition     = var.cache_expiry_days >= 1 && var.cache_expiry_days == floor(var.cache_expiry_days)
    error_message = "cache_expiry_days must be a positive integer."
  }
}
