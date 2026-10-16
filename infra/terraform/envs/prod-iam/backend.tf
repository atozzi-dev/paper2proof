# Same state bucket as envs/prod, separate key: this root is applied only by a human with
# tenant-admin rights, envs/prod also by CI. Credentials come from AWS_ACCESS_KEY_ID and
# AWS_SECRET_ACCESS_KEY in the environment, never from this file.

terraform {
  backend "s3" {
    bucket = "p2p-tfstate"
    key    = "prod-iam/terraform.tfstate"
    region = "eu-north1"

    endpoints = {
      s3 = "https://storage.eu-north1.nebius.cloud"
    }

    use_lockfile = true

    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
