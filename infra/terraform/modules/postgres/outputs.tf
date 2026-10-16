output "cluster_id" {
  description = "ID of the PostgreSQL cluster, or null when disabled."
  value       = one(nebius_msp_postgresql_v1alpha1_cluster.this[*].id)
}
