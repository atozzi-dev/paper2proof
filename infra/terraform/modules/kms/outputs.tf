output "key_id" {
  description = "ID of the bundle signing key."
  value       = nebius_kms_v1_asymmetric_key.signing.id
}
