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

variable "regions" {
  description = "Per-region forecast configuration (models, stages, sync spec)"
  type = map(object({
    models = list(string)
    stages = list(string)
    sync = object({
      rules     = list(string)
      git_push  = bool
      date_kind = string
    })
  }))
}

variable "full_field_models" {
  description = "Models whose full-field raw forecast should be uploaded to the common bucket"
  type        = set(string)
  default     = ["aifs", "aifs_ens"]
}

variable "pipeline_schedule" {
  description = "Cron schedule for pipeline trigger"
  type        = string
  default     = "*/15 * * * *"
}

variable "call_log_level" {
  description = "Workflow call log level (LOG_ALL_CALLS, LOG_ERRORS_ONLY, LOG_NONE)"
  type        = string
  default     = "LOG_NONE"
}

variable "execution_history_level" {
  description = "Workflow execution history level"
  type        = string
  default     = "EXECUTION_HISTORY_LEVEL_UNSPECIFIED"
}

variable "common_bucket" {
  description = "Common GCS bucket for ICs, weights, full-field forecasts, intermediate markers"
  type        = string
}

variable "region_buckets" {
  description = "Map of forecast region to region-specific GCS bucket"
  type        = map(string)
}

variable "pipeline_service_account_id" {
  description = "Full resource ID of the pipeline service account for impersonation binding"
  type        = string
}

variable "pipeline_service_account_email" {
  description = "Email of the pipeline service account used by Batch jobs"
  type        = string
}

variable "cloud_run_services" {
  description = "Map of Cloud Run job configurations"
  type = map(object({
    name = string
    id   = string
  }))
}

variable "pipeline_state_service_name" {
  description = "Cloud Run service name for the pipeline-state service"
  type        = string
}

variable "pipeline_state_url" {
  description = "Cloud Run service URL for the pipeline-state service"
  type        = string
}

variable "batch_job_template" {
  description = "Cloud Batch job template configuration"
  type = object({
    project        = string
    region         = string
    machine_type   = string
    gpu_type       = string
    gpu_count      = number
    os_image       = string
    boot_disk_gb   = number
    boot_disk_type = optional(string)
    max_attempts   = number
    model_resources = map(object({
      machine_type      = string
      boot_disk_size_gb = optional(number)
      boot_disk_type    = optional(string)
      cpu_milli         = optional(number)
      memory_mib        = optional(number)
      gpu_type          = optional(string)
      gpu_count         = optional(number)
    }))
    image_streaming = bool
    preemptible     = bool
    aifs_image      = string
    neuralgcm_image = string
    gencast_image   = string
    vpc_network     = string
    vpc_subnet      = string
  })
}

variable "gencast_tpu_dispatch_template" {
  description = "TPU dispatch template for GenCast inference"
  type = object({
    zone                   = string
    accelerator_type       = string
    runtime_version        = string
    spot                   = bool
    max_attempts           = number
    poll_interval_seconds  = number
    queue_timeout_seconds  = number
    run_timeout_seconds    = number
    request_valid_duration = string
    workload_image         = string
    artifact_registry_host = string
    global_device_count    = number
    local_device_count     = number
    process_count          = number
    vpc_network            = string
    vpc_subnet             = string
  })
}
