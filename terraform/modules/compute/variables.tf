# -----------------------------------------------------------------------------
# Compute Module Variables
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

# -----------------------------------------------------------------------------
# Network Configuration
# -----------------------------------------------------------------------------

variable "vpc_id" {
  description = "VPC network ID"
  type        = string
}

variable "vpc_subnetwork" {
  description = "VPC subnetwork ID"
  type        = string
}

# -----------------------------------------------------------------------------
# Forecast regions
# -----------------------------------------------------------------------------

variable "regions" {
  description = "Per-region forecast configuration (models, stages, sync spec)"
  type = map(object({
    models = list(string)
    stages = list(string)
    sync = object({
      rules = list(string)
      sources = list(object({
        gcs_prefix = string
        local_dir  = string
        date_kind  = string
      }))
      git_push  = bool
      date_kind = string
    })
  }))
}

# -----------------------------------------------------------------------------
# Storage Configuration
# -----------------------------------------------------------------------------

variable "common_gcs_bucket" {
  description = "Common GCS bucket for ICs, weights, full-field forecasts, intermediate markers"
  type        = string
}

variable "region_buckets" {
  description = "Map of forecast region to region-specific GCS bucket for post-processed and blended outputs"
  type        = map(string)
}

variable "service_account_email" {
  description = "Service account email for pipeline jobs"
  type        = string
}

# -----------------------------------------------------------------------------
# Container Images
# -----------------------------------------------------------------------------

variable "downloader_image" {
  description = "Container image for downloader service"
  type        = string
}

variable "pipeline_state_image" {
  description = "Container image for the pipeline-state service"
  type        = string
}

variable "postprocess_image" {
  description = "Container image for post-processing service"
  type        = string
}

variable "blend_image" {
  description = "Container image for blend service"
  type        = string
}

variable "sync_image" {
  description = "Container image for sync service"
  type        = string
}

variable "aifs_image" {
  description = "Container image used for both AIFS and AIFS-ENS inference (selected by MODEL env)"
  type        = string
}

variable "neuralgcm_image" {
  description = "Container image for NeuralGCM inference"
  type        = string
}

variable "gencast_image" {
  description = "Container image for GenCast inference"
  type        = string
}

# -----------------------------------------------------------------------------
# External API credentials (managed in Secret Manager via terraform)
# -----------------------------------------------------------------------------

variable "external_api_secrets" {
  description = <<-EOT
    Map of env-var name → secret value. Each entry becomes a Secret Manager
    secret (id = lower-kebab-cased env-var name), a version holding the value,
    an IAM binding granting the pipeline SA `secretAccessor`, and an env mount
    on whichever Cloud Run job declares the env-var name under its `secrets`
    field. Values are sensitive — pass via TF_VAR_external_api_secrets or a
    gitignored *.tfvars file. Empty map = no secrets created.
  EOT
  type        = map(string)
  default     = {}
  sensitive   = true
}

# -----------------------------------------------------------------------------
# GPU Configuration
# -----------------------------------------------------------------------------

variable "use_preemptible_gpu" {
  description = "Use preemptible/spot GPUs for cost savings"
  type        = bool
  default     = true
}

variable "gpu_type" {
  description = "GPU type for AIFS (e.g., nvidia-tesla-a100, nvidia-l4)"
  type        = string
  default     = "nvidia-tesla-a100"

  validation {
    condition     = var.gpu_type != "nvidia-a100-40gb"
    error_message = "Use the Compute Engine accelerator type name nvidia-tesla-a100 for A100 40GB GPUs, not nvidia-a100-40gb."
  }
}

variable "gpu_machine_type" {
  description = "Machine type for GPU Batch jobs. Leave empty to derive a compatible default from gpu_type."
  type        = string
  default     = ""
}

variable "batch_vm_os_image" {
  description = "Batch VM OS image for model jobs. batch-cos uses Batch Container-Optimized OS for container workloads."
  type        = string
  default     = "batch-cos"
}

variable "batch_boot_disk_size_gb" {
  description = "Boot disk size in GB for Cloud Batch GPU VMs. Must be large enough to unpack NVIDIA container image layers."
  type        = number
  default     = 100
}

variable "batch_enable_image_streaming" {
  description = "Enable Cloud Batch image streaming for model container runnables stored in Artifact Registry."
  type        = bool
  default     = false
}
