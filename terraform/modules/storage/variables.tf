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

variable "forecast_regions" {
  description = "List of forecast regions to create folder structure for"
  type        = list(string)
  default     = ["india"]
}

variable "retention_days" {
  description = "Days to retain raw/intermediate data before deletion"
  type        = number
  default     = 30
}

variable "enable_versioning" {
  description = "Enable versioning on main bucket"
  type        = bool
  default     = false
}

variable "archive_after_days" {
  description = "Days after which to move output data to NEARLINE storage (null to disable)"
  type        = number
  default     = null
}
