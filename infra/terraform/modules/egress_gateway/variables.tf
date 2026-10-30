variable "project_id" {
  description = "ID of the p2p-sandbox project."
  type        = string
}

variable "network_id" {
  description = "ID of net-sandbox (the security group belongs to it)."
  type        = string
}

variable "subnet_id" {
  description = "ID of the gateway subnet (default route table, public IP pool)."
  type        = string
}

variable "private_allocation_id" {
  description = "Private allocation gateway-private: the next hop of rt-provision and rt-exec."
  type        = string
}

variable "private_ip" {
  description = "IPv4 address of gateway-private (the gateway's own address on its NIC)."
  type        = string

  validation {
    condition     = can(cidrhost("${var.private_ip}/32", 0))
    error_message = "private_ip must be an IPv4 address."
  }
}

variable "provision_cidr" {
  description = "CIDR of the provision subnet: source of the provision policy."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.provision_cidr))
    error_message = "provision_cidr must be an IPv4 CIDR block."
  }
}

variable "exec_cidr" {
  description = "CIDR of the exec subnet: source of the exec policy."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.exec_cidr))
    error_message = "exec_cidr must be an IPv4 CIDR block."
  }
}

variable "storage_host" {
  description = "Object Storage endpoint host, substituted for $${NEBIUS_STORAGE_HOST} in the allowlist."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+(\\.[a-z0-9-]+)+$", var.storage_host))
    error_message = "storage_host must be a DNS name."
  }
}

variable "registry_host" {
  description = "Container Registry host, substituted for $${NEBIUS_REGISTRY_HOST} in the allowlist."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+(\\.[a-z0-9-]+)+$", var.registry_host))
    error_message = "registry_host must be a DNS name."
  }
}

variable "allowlist_yaml" {
  description = "Content of config/allowlist.yaml; the gateway renders squid.conf from its egress section."
  type        = string
}

variable "create_stopped" {
  description = "Create the VM stopped; only the value at creation matters (start and stop are the orchestrator's)."
  type        = bool
  default     = true
}

variable "name" {
  description = "Name and hostname of the gateway VM."
  type        = string
  default     = "p2p-egress-gw"
}

# cpu-e2 2vcpu-8gb is the cheapest preset in eu-north1 (architecture §8.2, §17).
variable "platform" {
  description = "Compute platform of the gateway VM."
  type        = string
  default     = "cpu-e2"
}

variable "preset" {
  description = "Compute preset of the gateway VM."
  type        = string
  default     = "2vcpu-8gb"
}

variable "image_family" {
  description = "Boot image family: Ubuntu 24.04 without GPU drivers."
  type        = string
  default     = "ubuntu24.04-driverless"
}

variable "disk_type" {
  description = "Boot disk type."
  type        = string
  default     = "NETWORK_SSD"
}

variable "disk_size_gibibytes" {
  description = "Boot disk size in GiB (billed while the VM is stopped too)."
  type        = number
  default     = 20

  validation {
    condition     = var.disk_size_gibibytes >= 20 && var.disk_size_gibibytes == floor(var.disk_size_gibibytes)
    error_message = "disk_size_gibibytes must be an integer >= 20."
  }
}
