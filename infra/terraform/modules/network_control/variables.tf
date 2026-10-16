variable "project_id" {
  description = "ID of the p2p-control project."
  type        = string
}

variable "network_name" {
  description = "Name of the control-plane network."
  type        = string
  default     = "net-control"
}

variable "subnet_name" {
  description = "Name of the control-plane subnet."
  type        = string
  default     = "subnet-control"
}
