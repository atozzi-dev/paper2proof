variable "enabled" {
  description = "Create the cluster (enable_managed_pg)."
  type        = bool
  default     = false
}

variable "project_id" {
  description = "ID of the project that owns the cluster (p2p-control)."
  type        = string
}

variable "network_id" {
  description = "Network the cluster is reachable from (net-control, never net-sandbox)."
  type        = string
}

variable "name" {
  description = "Cluster name."
  type        = string
  default     = "p2p-pg"
}

variable "db_name" {
  description = "Database name."
  type        = string
  default     = "p2p"
}

variable "user_name" {
  description = "Bootstrap user name."
  type        = string
  default     = "p2p"
}

variable "password" {
  description = "Bootstrap user password (write-only, set through TF_VAR_pg_password). Required when enabled."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true
}

variable "password_version" {
  description = "Bump to rotate the write-only password."
  type        = string
  default     = "1"
}

variable "platform" {
  description = "Host platform."
  type        = string
  default     = "cpu-e2"
}

variable "preset" {
  description = "Host preset."
  type        = string
  default     = "2vcpu-8gb"
}

variable "disk_type" {
  description = "Disk type."
  type        = string
  default     = "network_ssd"
}

variable "disk_size_gibibytes" {
  description = "Disk size in GiB."
  type        = number
  default     = 20
}
