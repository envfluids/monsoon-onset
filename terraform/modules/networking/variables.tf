# -----------------------------------------------------------------------------
# Networking Module Variables
# -----------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
}

variable "environment" {
  description = "Environment name (dev, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
  default     = "monsoon"
}

variable "subnet_cidr" {
  description = "CIDR range for the main subnet"
  type        = string
  default     = "10.0.0.0/20"
}

variable "additional_subnets" {
  description = "Additional regional subnets to create on the same VPC, keyed by logical name"
  type = map(object({
    region = string
    cidr   = string
  }))
  default = {}
}
