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
  default     = "*/15 * * * *"  # Every 15 minutes
}

variable "call_log_level" {
  description = "Workflow call log level for execution history (LOG_ALL_CALLS, LOG_ERRORS_ONLY, LOG_NONE)"
  type        = string
  default     = "LOG_NONE"
}

variable "weights_bucket" {
  description = "GCS bucket name for model weights and large static files"
  type        = string
  default     = ""
}

variable "pipeline_service_account_id" {
  description = "Full resource ID of the pipeline service account for impersonation binding"
  type        = string
}

variable "tpu_service_account_id" {
  description = "Full resource ID of the TPU service account for impersonation binding"
  type        = string
}

variable "cloud_run_services" {
  description = "Map of Cloud Run job configurations"
  type = map(object({
    name = string
    id   = string
  }))
}

variable "batch_job_template" {
  description = "Cloud Batch job template configuration"
  type = object({
    project      = string
    region       = string
    machine_type = string
    gpu_type     = string
    gpu_count    = number
    preemptible  = bool
    image        = string
    vpc_network     = string
    vpc_subnet      = string
    tpu_type        = string
    neuralgcm_image = string
  })
}
