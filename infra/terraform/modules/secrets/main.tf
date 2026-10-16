# SecretStash container only (architecture §9.2, §12.1). Terraform never creates a version or a
# payload: that would put the value in the state (invariant 4). Values are loaded out of band by
# deploy/secrets.sh. tests/infra/test_no_secret_versions.py enforces this. Who may read the
# payload is granted in envs/prod-iam (project_iam).

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.64, < 0.7.0"
    }
  }
}

resource "nebius_mysterybox_v1_secret" "this" {
  parent_id   = var.project_id
  name        = var.name
  description = "Paper2Proof control-plane secrets. Values are loaded by deploy/secrets.sh, never by Terraform."
}

