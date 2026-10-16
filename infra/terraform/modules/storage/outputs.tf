output "bucket_names" {
  description = "Static bucket names by role: state, bundles, cache."
  value = {
    state   = nebius_storage_v1_bucket.state.name
    bundles = nebius_storage_v1_bucket.bundles.name
    cache   = nebius_storage_v1_bucket.cache.name
  }
}

output "bucket_ids" {
  description = "Static bucket IDs by role: state, bundles, cache."
  value = {
    state   = nebius_storage_v1_bucket.state.id
    bundles = nebius_storage_v1_bucket.bundles.id
    cache   = nebius_storage_v1_bucket.cache.id
  }
}
