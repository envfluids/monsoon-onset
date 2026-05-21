# -----------------------------------------------------------------------------
# Storage Module Variables
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

variable "regions" {
  description = "Per-region forecast configuration. Only the keys are used to create per-region buckets."
  type        = map(any)
  default     = {}
}

variable "retention_days" {
  description = "Days to retain ic/intermediate data before deletion"
  type        = number
  default     = 30
}

variable "enable_versioning" {
  description = "Enable versioning on common bucket"
  type        = bool
  default     = false
}

variable "archive_after_days" {
  description = "Days after which to move full_field data to NEARLINE storage (null to disable)"
  type        = number
  default     = null
}

variable "artifact_registry_cleanup_older_than" {
  description = "Artifact Registry cleanup threshold for old container image versions in dev. Duration string such as 604800s for 7 days."
  type        = string
  default     = "604800s"
}
