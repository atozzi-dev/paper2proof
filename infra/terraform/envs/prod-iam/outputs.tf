output "service_account_ids" {
  description = "Service account IDs by role."
  value       = module.project_iam.service_account_ids
}

output "group_ids" {
  description = "Group IDs by role."
  value       = module.project_iam.group_ids
}

output "github_oidc_subject" {
  description = "OIDC sub claim accepted for sa-ci-deployer (null when disabled)."
  value       = module.project_iam.github_oidc_subject
}

output "signing_key_id" {
  description = "ID of the KMS key that signs Proof Bundles."
  value       = module.kms.key_id
}

# Key IDs and SecretStash references, never the secret halves (they are delivered to SecretStash).
output "s3_access_keys" {
  description = "S3 access key IDs and SecretStash references for sa-api-runtime and sa-job-launcher."
  sensitive   = true
  value = {
    api_runtime  = module.project_iam.api_runtime_s3_key
    job_launcher = module.project_iam.job_launcher_s3_key
  }
}
