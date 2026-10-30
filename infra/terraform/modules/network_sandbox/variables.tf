variable "project_id" {
  description = "ID of the p2p-sandbox project."
  type        = string
}

variable "network_name" {
  description = "Name of the sandbox network."
  type        = string
  default     = "net-sandbox"
}

# The CIDRs must be inside the private pool that Nebius creates for a new network (assumed to be
# the region's default range, 10.0.0.0/13 in eu-north1; checked at H-04 apply). They are fixed
# rather than auto-allocated so the gateway (A-04) can build per-subnet Squid ACLs from them.
variable "provision_cidr" {
  description = "CIDR of the provision subnet (PROVISION jobs)."
  type        = string
  default     = "10.1.0.0/24"

  validation {
    condition     = can(cidrnetmask(var.provision_cidr))
    error_message = "provision_cidr must be an IPv4 CIDR block."
  }
}

variable "exec_cidr" {
  description = "CIDR of the exec subnet (EXECUTE GPU jobs)."
  type        = string
  default     = "10.1.1.0/24"

  validation {
    condition     = can(cidrnetmask(var.exec_cidr))
    error_message = "exec_cidr must be an IPv4 CIDR block."
  }
}

variable "gateway_cidr" {
  description = "CIDR of the gateway subnet (egress gateway VM only)."
  type        = string
  default     = "10.1.2.0/27"

  validation {
    condition     = can(cidrnetmask(var.gateway_cidr))
    error_message = "gateway_cidr must be an IPv4 CIDR block."
  }
}

variable "gateway_host_index" {
  description = "Host number of the gateway's private IP inside gateway_cidr (low addresses are left to the platform)."
  type        = number
  default     = 10

  validation {
    condition     = var.gateway_host_index >= 4 && var.gateway_host_index == floor(var.gateway_host_index)
    error_message = "gateway_host_index must be an integer >= 4."
  }
}
