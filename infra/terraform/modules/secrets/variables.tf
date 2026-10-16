variable "project_id" {
  description = "ID of the project that owns the secret (p2p-control)."
  type        = string
}

variable "name" {
  description = "Secret name."
  type        = string
  default     = "p2p-secrets"
}
