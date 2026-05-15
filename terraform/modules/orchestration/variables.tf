# -----------------------------------------------------------------------------
# Orchestration Module Variables
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
  description = "List of forecast regions to schedule"
  type        = list(string)
  default     = ["india"]
}

variable "pipeline_schedule" {
  description = "Cron schedule for pipeline trigger"
  type        = string
  default     = "*/15 * * * *" # Every 15 minutes
}

variable "call_log_level" {
  description = "Workflow call log level for execution history (LOG_ALL_CALLS, LOG_ERRORS_ONLY, LOG_NONE)"
  type        = string
  default     = "LOG_NONE"
}

variable "execution_history_level" {
  description = "Workflow execution history level (EXECUTION_HISTORY_LEVEL_UNSPECIFIED, EXECUTION_HISTORY_BASIC, EXECUTION_HISTORY_DETAILED)"
  type        = string
  default     = "EXECUTION_HISTORY_LEVEL_UNSPECIFIED"
}

variable "weights_bucket" {
  description = "GCS bucket name for model weights and large static files"
  type        = string
  default     = ""
}

variable "common_bucket" {
  description = "Common GCS bucket for ICs, intermediate markers, and raw forecasts"
  type        = string
  default     = ""
}

variable "region_buckets" {
  description = "Map of forecast region to region-specific GCS bucket for post-processed and blended outputs"
  type        = map(string)
  default     = {}
}

variable "pipeline_service_account_id" {
  description = "Full resource ID of the pipeline service account for impersonation binding"
  type        = string
}

variable "pipeline_service_account_email" {
  description = "Email address of the pipeline service account used by Batch jobs"
  type        = string
}

variable "cloud_run_services" {
  description = "Map of Cloud Run job configurations"
  type = map(object({
    name = string
    id   = string
  }))
}

variable "pipeline_state_service" {
  description = "Cloud Run service metadata for the pipeline-state service"
  type = object({
    name = string
    uri  = string
  })
}

variable "batch_job_template" {
  description = "Cloud Batch job template configuration"
  type = object({
    project         = string
    region          = string
    machine_type    = string
    gpu_type        = string
    gpu_count       = number
    os_image        = string
    boot_disk_gb    = number
    image_streaming = bool
    preemptible     = bool
    image           = string
    vpc_network     = string
    vpc_subnet      = string
    neuralgcm_image = string
  })
}
