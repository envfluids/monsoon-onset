# -----------------------------------------------------------------------------
# Production Environment
# -----------------------------------------------------------------------------

terraform {
  backend "gcs" {
    # Configure via: terraform init -backend-config="bucket=<your-tf-state-bucket>"
    prefix = "monsoon/prod"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# -----------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "notification_emails" {
  description = "Email addresses for alerts"
  type        = list(string)
  default     = []
}

locals {
  environment      = "prod"
  forecast_regions = ["india"]  # Add more regions as needed
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

module "networking" {
  source = "../../modules/networking"

  project_id  = var.project_id
  region      = var.region
  environment = local.environment
}

# -----------------------------------------------------------------------------
# Storage
# -----------------------------------------------------------------------------

module "storage" {
  source = "../../modules/storage"

  project_id       = var.project_id
  region           = var.region
  environment      = local.environment
  forecast_regions = local.forecast_regions

  # Prod: longer retention, archival enabled
  retention_days     = 365
  enable_versioning  = true
  archive_after_days = 90
}

# -----------------------------------------------------------------------------
# Compute
# -----------------------------------------------------------------------------

module "compute" {
  source = "../../modules/compute"

  project_id  = var.project_id
  region      = var.region
  environment = local.environment

  vpc_id         = module.networking.vpc_id
  vpc_subnetwork = module.networking.subnetwork_id

  # Prod: on-demand instances for reliability
  use_preemptible_gpu = false
  tpu_type            = "v4-8"  # Production TPU

  # Container images (pinned versions in prod)
  downloader_image   = "gcr.io/${var.project_id}/monsoon-downloader:v1.0.0"
  postprocess_image  = "gcr.io/${var.project_id}/monsoon-postprocess:v1.0.0"
  blend_image        = "gcr.io/${var.project_id}/monsoon-blend:v1.0.0"
  sync_image         = "gcr.io/${var.project_id}/monsoon-sync:v1.0.0"
  aifs_image         = "gcr.io/${var.project_id}/monsoon-aifs:v1.0.0"
  neuralgcm_image    = "gcr.io/${var.project_id}/monsoon-neuralgcm:v1.0.0"

  depends_on = [module.networking, module.storage]
}

# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

module "orchestration" {
  source = "../../modules/orchestration"

  project_id  = var.project_id
  region      = var.region
  environment = local.environment

  forecast_regions = local.forecast_regions

  # Prod: frequent runs matching current HPC schedule
  pipeline_schedule = "*/15 * * * *"  # Every 15 minutes (checks for new data)

  cloud_run_services = module.compute.cloud_run_services
  batch_job_template = module.compute.batch_job_template

  depends_on = [module.compute]
}

# -----------------------------------------------------------------------------
# Monitoring
# -----------------------------------------------------------------------------

module "monitoring" {
  source = "../../modules/monitoring"

  project_id  = var.project_id
  environment = local.environment

  # Prod: full alerting
  enable_alerts       = true
  notification_emails = var.notification_emails

  depends_on = [module.orchestration]
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "storage_bucket" {
  description = "Main storage bucket name"
  value       = module.storage.bucket_name
}

output "workflow_url" {
  description = "Cloud Workflow execution URL"
  value       = module.orchestration.workflow_url
}

output "monitoring_dashboard_url" {
  description = "Cloud Monitoring dashboard URL"
  value       = module.monitoring.dashboard_url
}
