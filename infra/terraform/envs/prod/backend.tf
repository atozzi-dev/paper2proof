# Remote state in the p2p-tfstate bucket (created by hand at bootstrap, versioning on), using the
# S3 backend against Nebius Object Storage. No credentials here: export AWS_ACCESS_KEY_ID and
# AWS_SECRET_ACCESS_KEY for an access key that can write the bucket before `terraform init`.
# Validation runs with `terraform init -backend=false` and never touches this backend.

terraform {
  backend "s3" {
    bucket = "p2p-tfstate"
    key    = "prod/terraform.tfstate"
    region = "eu-north1"

    endpoints = {
      s3 = "https://storage.eu-north1.nebius.cloud"
    }

    # S3-native lock file (Terraform >= 1.10); no DynamoDB on Nebius.
    use_lockfile = true

    # AWS-only checks that do not apply to Nebius Object Storage.
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
