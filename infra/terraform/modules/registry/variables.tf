variable "project_id" {
  description = "ID of the project that owns the registry."
  type        = string
}

variable "name" {
  description = "Registry name."
  type        = string
}

variable "description" {
  description = "Registry description."
  type        = string
  default     = ""
}
