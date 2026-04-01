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

variable "forecast_regions" {
  description = "List of forecast regions to support (e.g., india, west_africa)"
  type        = list(string)
  default     = ["india"]
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
  # Standard naming: {prefix}-{environment}-{resource}
  resource_prefix = "${var.name_prefix}-${var.environment}"

  # Common labels applied to all resources
  common_labels = {
    project     = var.name_prefix
    environment = var.environment
    managed_by  = "terraform"
  }
}
