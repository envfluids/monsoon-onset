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

variable "grant_pipeline_batch_job_viewer" {
  description = "Give the pipeline service account roles/batch.jobsViewer, so pipeline-state can read Batch job states for the ensemble fallback ladder."
  type        = bool
  default     = false
}

variable "external_share_readers" {
  description = "External read-only collaborators for the share bucket. Key: short id used in the service account name (ext-<key>-reader). Value: display name. An empty map creates no share bucket."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.external_share_readers) : can(regex("^[a-z][a-z0-9-]{0,18}$", k))])
    error_message = "Reader keys must start with a lowercase letter, use only lowercase letters, digits or hyphens, and be at most 19 characters (service account IDs are limited to 30)."
  }
}

variable "external_share_uploaders" {
  description = "IAM members (e.g. user:name@uchicago.edu) who can upload to and manage objects in the share bucket."
  type        = list(string)
  default     = []
}
