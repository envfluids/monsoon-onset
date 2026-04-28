# -----------------------------------------------------------------------------
# Development Environment
# -----------------------------------------------------------------------------

terraform {
  backend "gcs" {
    # Configure via: terraform init -backend-config="bucket=<your-tf-state-bucket>"
    prefix = "monsoon/dev"
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

locals {
  environment      = "dev"
  forecast_regions = ["india"]
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

  # Dev: shorter retention, no archival
  retention_days         = 30
  enable_versioning      = false
  archive_after_days     = null
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

  gcs_bucket            = module.storage.bucket_name
  weights_bucket        = module.storage.weights_bucket_name
  service_account_email = module.storage.pipeline_service_account_email

  # Dev: smaller instances, preemptible
  use_preemptible_gpu = true
  tpu_type            = "v3-8"  # Smaller TPU for dev

  # Container images — pulled from Artifact Registry created by storage module
  downloader_image   = "${module.storage.artifact_registry_url}/monsoon-downloader:latest"
  postprocess_image  = "${module.storage.artifact_registry_url}/monsoon-postprocess:latest"
  blend_image        = "${module.storage.artifact_registry_url}/monsoon-blend:latest"
  sync_image         = "${module.storage.artifact_registry_url}/monsoon-sync:latest"
  aifs_image         = "${module.storage.artifact_registry_url}/monsoon-aifs:latest"
  neuralgcm_image    = "${module.storage.artifact_registry_url}/monsoon-neuralgcm:latest"

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

  # Dev: less frequent runs
  pipeline_schedule = "0 */6 * * *"  # Every 6 hours
  call_log_level    = "LOG_ALL_CALLS"

  cloud_run_services = module.compute.cloud_run_services
  batch_job_template = module.compute.batch_job_template
  weights_bucket     = module.storage.weights_bucket_name

  pipeline_service_account_id = module.storage.pipeline_service_account_name
  tpu_service_account_id      = module.compute.tpu_service_account_id

  depends_on = [module.compute]
}

# -----------------------------------------------------------------------------
# Monitoring
# -----------------------------------------------------------------------------

module "monitoring" {
  source = "../../modules/monitoring"

  project_id  = var.project_id
  region      = var.region
  environment = local.environment

  # Dev: minimal alerting
  enable_alerts       = false
  notification_emails = []

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
