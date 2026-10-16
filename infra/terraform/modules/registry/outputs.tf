output "id" {
  description = "Registry ID (registry-...)."
  value       = nebius_registry_v1_registry.this.id
}

output "host" {
  description = "Regional registry host, e.g. cr.eu-north1.nebius.cloud (NEBIUS_REGISTRY_HOST)."
  value       = nebius_registry_v1_registry.this.status.registry_fqdn
}

# The registry path is the ID without its `registry-` prefix (Container Registry docs).
output "url" {
  description = "Image prefix for docker push/pull: <host>/<path> (REGISTRY_CONTROL / REGISTRY_SANDBOX)."
  value       = "${nebius_registry_v1_registry.this.status.registry_fqdn}/${trimprefix(nebius_registry_v1_registry.this.id, "registry-")}"
}
