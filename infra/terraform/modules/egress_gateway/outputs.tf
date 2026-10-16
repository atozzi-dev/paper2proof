# The orchestrator starts and stops the gateway. The instance ID changes whenever the VM is
# replaced, so it should resolve the VM by name (GATEWAY_NAME) rather than keep the ID.

output "instance_id" {
  description = "ID of the gateway VM (GATEWAY_INSTANCE_ID)."
  value       = nebius_compute_v1_instance.gateway.id
}

output "name" {
  description = "Name of the gateway VM in p2p-sandbox (GATEWAY_NAME), stable across replacements."
  value       = nebius_compute_v1_instance.gateway.name
}

output "security_group_id" {
  description = "ID of sg-egress-gw."
  value       = nebius_vpc_v1_security_group.gateway.id
}
