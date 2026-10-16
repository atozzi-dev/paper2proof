# tflint configuration for infra/terraform (used by `make tf-check`).
# The Nebius provider has no tflint ruleset; the bundled Terraform ruleset checks style,
# documentation, typing and module structure.

config {
  # Lint local modules through their calls in envs/prod as well as on their own.
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "all"
}
