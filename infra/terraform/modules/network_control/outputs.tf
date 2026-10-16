output "network_id" {
  description = "ID of net-control."
  value       = nebius_vpc_v1_network.control.id
}

output "subnet_id" {
  description = "ID of the control-plane subnet (SUBNET_CONTROL_ID)."
  value       = nebius_vpc_v1_subnet.control.id
}
