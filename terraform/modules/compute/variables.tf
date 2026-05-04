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
# Storage Configuration
# -----------------------------------------------------------------------------

variable "gcs_bucket" {
  description = "Main GCS bucket name"
  type        = string
  default     = ""
}

variable "weights_bucket" {
  description = "GCS bucket for model weights and large static files"
  type        = string
  default     = ""
}

variable "service_account_email" {
  description = "Service account email for pipeline jobs"
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# Container Images
# -----------------------------------------------------------------------------

variable "downloader_image" {
  description = "Container image for downloader service"
  type        = string
}

variable "pipeline_state_image" {
  description = "Container image for the pipeline-state service (IC discovery + GCS state probes)"
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
  description = "Container image for AIFS inference"
  type        = string
}

variable "neuralgcm_image" {
  description = "Container image for NeuralGCM inference"
  type        = string
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
