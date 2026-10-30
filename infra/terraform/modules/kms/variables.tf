variable "project_id" {
  description = "ID of the project that owns the key (p2p-signing, nothing else in it)."
  type        = string
}

variable "name" {
  description = "Key name."
  type        = string
  default     = "p2p-bundle-signing"
}

variable "algorithm" {
  description = "Signing algorithm. Immutable after creation."
  type        = string
  default     = "ECDSA_NIST_P256_SHA_256"

  validation {
    condition     = contains(["ECDSA_NIST_P256_SHA_256", "ECDSA_NIST_P384_SHA_384"], var.algorithm)
    error_message = "algorithm must be a signing algorithm (ECDSA P-256 or P-384)."
  }
}
