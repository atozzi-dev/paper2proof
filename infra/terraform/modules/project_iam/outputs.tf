output "service_account_ids" {
  description = "Service account IDs by role (ci_deployer, api_runtime, job_launcher)."
  value = {
    ci_deployer  = nebius_iam_v1_service_account.ci_deployer.id
    api_runtime  = nebius_iam_v1_service_account.api_runtime.id
    job_launcher = nebius_iam_v1_service_account.job_launcher.id
  }
}

output "group_ids" {
  description = "Group IDs by role."
  value = {
    ci_deployer  = nebius_iam_v1_group.ci_deployer.id
    api_runtime  = nebius_iam_v1_group.api_runtime.id
    job_launcher = nebius_iam_v1_group.job_launcher.id
  }
}

output "github_oidc_subject" {
  description = "OIDC sub claim accepted for sa-ci-deployer, or null when OIDC is disabled."
  value       = var.enable_github_oidc ? local.github_oidc_subject : null
}

# Key IDs and SecretStash references only: the secret halves never reach the state. Marked
# sensitive anyway so they are not printed by plan/apply; read them with `terraform output -json`
# into deploy/secrets.sh, never into a file.
output "api_runtime_s3_key" {
  description = "S3 access key of sa-api-runtime: AWS-style key ID and the SecretStash secret holding its secret."
  sensitive   = true
  value = {
    key_id              = nebius_iam_v2_access_key.api_runtime.status.aws_access_key_id
    secret_reference_id = nebius_iam_v2_access_key.api_runtime.status.secret_reference_id
  }
}

output "job_launcher_s3_key" {
  description = "S3 access key of sa-job-launcher: AWS-style key ID and the SecretStash secret holding its secret."
  sensitive   = true
  value = {
    key_id              = nebius_iam_v2_access_key.job_launcher.status.aws_access_key_id
    secret_reference_id = nebius_iam_v2_access_key.job_launcher.status.secret_reference_id
  }
}
