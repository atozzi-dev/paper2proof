# Everything the egress gateway module (A-04) needs: where the VM goes, which allocation it must
# carry, and the source CIDRs its per-subnet Squid ACLs and security rules match on.

output "network_id" {
  description = "ID of net-sandbox."
  value       = nebius_vpc_v1_network.sandbox.id
}

output "subnet_ids" {
  description = "Subnet IDs by name: provision (SUBNET_PROVISION_ID), exec (SUBNET_EXEC_ID), gateway."
  value = {
    provision = nebius_vpc_v1_subnet.routed["provision"].id
    exec      = nebius_vpc_v1_subnet.routed["exec"].id
    gateway   = nebius_vpc_v1_subnet.gateway.id
  }
}

output "subnet_cidrs" {
  description = "Subnet CIDRs by name, the source ranges of the gateway policies."
  value = {
    provision = var.provision_cidr
    exec      = var.exec_cidr
    gateway   = var.gateway_cidr
  }
}

output "route_table_ids" {
  description = "Custom route table IDs: rt-provision and rt-exec."
  value       = { for k, rt in nebius_vpc_v1_route_table.routed : k => rt.id }
}

output "gateway_allocation_id" {
  description = "Private allocation the default routes point at; A-04 attaches it to the gateway VM's NIC."
  value       = nebius_vpc_v1_allocation.gateway.id
}

output "gateway_private_ip" {
  description = "Private IP of the gateway (next hop of rt-provision and rt-exec)."
  value       = local.gateway_private_ip
}
