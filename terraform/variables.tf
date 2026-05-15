# -----------------------------------------------------------------------------
# Common Variables
# Used across all modules and environments
# -----------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for resources"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name (dev, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Environment must be 'dev' or 'prod'."
  }
}

# -----------------------------------------------------------------------------
# Forecast regions — single source of truth for which regions run which models
# and stages, plus per-region sync configuration.
# -----------------------------------------------------------------------------

variable "regions" {
  description = "Per-region forecast configuration (models, downstream stages, sync spec)"
  type = map(object({
    models = list(string) # which models produce output for this region
    stages = list(string) # which post-model stages run: "blend", "sync"
    sync = object({
      rules = list(string) # sync.yaml rule names to invoke
      sources = list(object({
        gcs_prefix = string # may contain {date} or {aifs_date}
        local_dir  = string # may contain {date} or {aifs_date}
        date_kind  = string # "date" or "aifs_date"
      }))
      git_push  = bool   # push to monsoon-operational repo
      date_kind = string # "date" (NeuralGCM-paced) or "aifs_date" (AIFS-only)
    })
  }))
  default = {}
}

variable "full_field_models" {
  description = "Models whose full-field raw forecast should be uploaded to the common bucket"
  type        = set(string)
  default     = ["aifs", "aifs_ens"]
}

# -----------------------------------------------------------------------------
# Naming Convention
# -----------------------------------------------------------------------------

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
  default     = "monsoon"
}

locals {
  resource_prefix = "${var.name_prefix}-${var.environment}"

  common_labels = {
    project     = var.name_prefix
    environment = var.environment
    managed_by  = "terraform"
  }
}
