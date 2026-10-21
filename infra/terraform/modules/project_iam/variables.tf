variable "tenant_id" {
  description = "Nebius tenant ID (tenant-...). Parent of the p2p-ci-deployer group."
  type        = string

  validation {
    condition     = can(regex("^tenant-[a-z0-9]+$", var.tenant_id))
    error_message = "tenant_id must look like tenant-<id>."
  }
}

variable "control_project_id" {
  description = "ID of the p2p-control project (project-...)."
  type        = string

  validation {
    condition     = can(regex("^project-[a-z0-9]+$", var.control_project_id))
    error_message = "control_project_id must look like project-<id>."
  }
}

variable "sandbox_project_id" {
  description = "ID of the p2p-sandbox project (project-...). The only project sa-job-launcher can act in."
  type        = string

  validation {
    condition     = can(regex("^project-[a-z0-9]+$", var.sandbox_project_id))
    error_message = "sandbox_project_id must look like project-<id>."
  }
}

variable "signing_project_id" {
  description = "ID of the p2p-signing project (project-...), which holds only the bundle signing key."
  type        = string

  validation {
    condition     = can(regex("^project-[a-z0-9]+$", var.signing_project_id))
    error_message = "signing_project_id must look like project-<id>."
  }
}

# Resource IDs, never a project or tenant ID: a permit on a project would be project-wide.
variable "api_runtime_bucket_ids" {
  description = "Buckets whose objects sa-api-runtime reads and writes (p2p-state, p2p-bundles), keyed by a static label."
  type        = map(string)

  validation {
    condition     = alltrue([for id in values(var.api_runtime_bucket_ids) : !can(regex("^(project|tenant)-", id))])
    error_message = "api_runtime_bucket_ids must hold bucket IDs, not project or tenant IDs."
  }
}

variable "secrets_id" {
  description = "ID of the p2p-secrets SecretStash secret whose payload sa-api-runtime may read."
  type        = string

  validation {
    condition     = !can(regex("^(project|tenant)-", var.secrets_id))
    error_message = "secrets_id must be a secret ID, not a project or tenant ID."
  }
}

variable "enable_github_oidc" {
  description = "Create the GitHub OIDC federated credential for sa-ci-deployer. Spike S-7 passed; set false only to fall back to a key in a GitHub environment)."
  type        = bool
  default     = true
}

variable "github_repo" {
  description = "GitHub repository allowed to impersonate sa-ci-deployer, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo must be owner/name."
  }
}

variable "github_environment" {
  description = "GitHub environment (required reviewers, deployment branches = main) whose jobs may deploy."
  type        = string
  default     = "prod"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.github_environment))
    error_message = "github_environment must be a plain environment name."
  }
}

variable "github_ref" {
  description = "Git ref allowed to deploy. Part of the OIDC sub claim once the repository's subject template includes ref."
  type        = string
  default     = "refs/heads/main"

  validation {
    condition     = can(regex("^refs/heads/[A-Za-z0-9._/-]+$", var.github_ref))
    error_message = "github_ref must be a branch ref (refs/heads/<name>) without wildcards."
  }
}

variable "github_owner_id" {
  description = "Numeric GitHub ID of the repository owner. GitHub signs it into the sub claim (repo:<owner>@<owner id>/<name>@<repo id>:..., spike S-7). Null keeps the name-only form."
  type        = string
  default     = null

  validation {
    condition     = var.github_owner_id == null || can(regex("^[0-9]+$", var.github_owner_id))
    error_message = "github_owner_id must be the numeric GitHub user or organisation ID."
  }
}

variable "github_repo_id" {
  description = "Numeric GitHub ID of the repository, signed into the sub claim with github_owner_id."
  type        = string
  default     = null

  validation {
    condition     = var.github_repo_id == null || can(regex("^[0-9]+$", var.github_repo_id))
    error_message = "github_repo_id must be the numeric GitHub repository ID."
  }
}

variable "github_oidc_subject" {
  description = "Override of the expected OIDC sub claim. Null means repo:<owner>[@<owner id>]/<name>[@<repo id>]:environment:<github_environment>:ref:<github_ref>."
  type        = string
  default     = null

  validation {
    condition     = var.github_oidc_subject == null || can(regex("^repo:[A-Za-z0-9-]+(@[0-9]+)?/[A-Za-z0-9._-]+(@[0-9]+)?:environment:[A-Za-z0-9_-]+:ref:refs/heads/[A-Za-z0-9._/-]+$", var.github_oidc_subject))
    error_message = "github_oidc_subject must pin repository, environment and branch: repo:<owner>[@<id>]/<name>[@<id>]:environment:<env>:ref:refs/heads/<branch>."
  }
}

variable "github_oidc_issuer" {
  description = "OIDC issuer of GitHub Actions tokens."
  type        = string
  default     = "https://token.actions.githubusercontent.com"
}
