variable "tenant_id" {
  description = "Nebius tenant ID (tenant-...)."
  type        = string
}

variable "control_project_id" {
  description = "ID of the p2p-control project."
  type        = string
}

variable "sandbox_project_id" {
  description = "ID of the p2p-sandbox project."
  type        = string
}

variable "signing_project_id" {
  description = "ID of the p2p-signing project (only the bundle signing key)."
  type        = string
}

variable "state_bucket_name" {
  description = "Name of the Litestream bucket (as in envs/prod)."
  type        = string
  default     = "p2p-state"
}

variable "bundles_bucket_name" {
  description = "Name of the Proof Bundle bucket (as in envs/prod)."
  type        = string
  default     = "p2p-bundles"
}

variable "secrets_name" {
  description = "Name of the SecretStash secret created by envs/prod."
  type        = string
  default     = "p2p-secrets"
}

variable "github_repo" {
  description = "GitHub repository (owner/name) whose prod environment deploys through OIDC."
  type        = string
  default     = "atozzi-dev/paper2proof"
}

variable "github_environment" {
  description = "GitHub environment allowed to deploy."
  type        = string
  default     = "prod"
}

variable "enable_github_oidc" {
  description = "Create the GitHub OIDC federated credential (false if spike S-7 fails)."
  type        = bool
  default     = true
}
