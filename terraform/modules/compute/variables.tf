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

variable "vpc_connector_id" {
  description = "VPC connector ID for Cloud Run"
  type        = string
  default     = null
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
# GPU/TPU Configuration
# -----------------------------------------------------------------------------

variable "use_preemptible_gpu" {
  description = "Use preemptible/spot GPUs for cost savings"
  type        = bool
  default     = true
}

variable "tpu_type" {
  description = "TPU type for NeuralGCM (e.g., v3-8, v4-8)"
  type        = string
  default     = "v3-8"
}

variable "gpu_type" {
  description = "GPU type for AIFS (e.g., nvidia-tesla-a100, nvidia-l4)"
  type        = string
  default     = "nvidia-tesla-a100"
}
