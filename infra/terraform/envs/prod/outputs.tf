# Non-secret identifiers for .env and the GitHub repository variables (H-04 step 5).

output "env" {
  description = "Values for .env / GitHub repository variables, keyed by variable name."
  value = {
    NEBIUS_CONTROL_PROJECT_ID = var.control_project_id
    NEBIUS_SANDBOX_PROJECT_ID = var.sandbox_project_id
    NEBIUS_REGION             = var.region
    SUBNET_CONTROL_ID         = module.network_control.subnet_id
    SUBNET_PROVISION_ID       = module.network_sandbox.subnet_ids.provision
    SUBNET_EXEC_ID            = module.network_sandbox.subnet_ids.exec
    REGISTRY_CONTROL          = module.registry_control.url
    REGISTRY_SANDBOX          = module.registry_sandbox.url
    NEBIUS_REGISTRY_HOST      = module.registry_control.host
    NEBIUS_STORAGE_HOST       = "storage.${var.region}.nebius.cloud"
  }
}

output "network_sandbox" {
  description = "net-sandbox layout for the egress gateway module (A-04)."
  value = {
    network_id            = module.network_sandbox.network_id
    subnet_ids            = module.network_sandbox.subnet_ids
    subnet_cidrs          = module.network_sandbox.subnet_cidrs
    route_table_ids       = module.network_sandbox.route_table_ids
    gateway_allocation_id = module.network_sandbox.gateway_allocation_id
    gateway_private_ip    = module.network_sandbox.gateway_private_ip
  }
}

output "buckets" {
  description = "Static bucket names by role."
  value       = module.storage.bucket_names
}

output "secrets_id" {
  description = "ID of the p2p-secrets SecretStash secret filled by deploy/secrets.sh."
  value       = module.secrets.secret_id
}

output "postgres_cluster_id" {
  description = "Managed PostgreSQL cluster ID, null unless enable_managed_pg."
  value       = module.postgres.cluster_id
}
