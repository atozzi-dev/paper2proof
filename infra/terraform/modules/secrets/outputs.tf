output "secret_id" {
  description = "ID of the SecretStash secret that deploy/secrets.sh fills."
  value       = nebius_mysterybox_v1_secret.this.id
}
