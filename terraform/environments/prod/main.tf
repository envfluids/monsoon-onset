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
  environment = "prod"

  regions = {
    india = {
      models = ["aifs", "neuralgcm"]
      stages = ["blend", "sync"]
      sync = {
        rules = ["blend_google"]
        sources = [
          {
            gcs_prefix = "output/blend/{date}/"
            local_dir  = "blend/output_google/india/{date}/"
            date_kind  = "date"
          },
        ]
        git_push  = true
        date_kind = "date"
      }
    }
    ethiopia = {
      models = ["aifs", "aifs_ens", "gencast"]
      stages = ["sync"]
      sync = {
        rules = ["AIFS", "AIFS_ENS", "GenCast"]
        sources = [
          {
            gcs_prefix = "output/aifs/{aifs_date}/AIFS/"
            local_dir  = "AIFS/output/ethiopia/AIFS/"
            date_kind  = "aifs_date"
          },
          {
            gcs_prefix = "output/aifs_ens/{aifs_date}/AIFS_ENS/"
            local_dir  = "AIFS/output/ethiopia/AIFS_ENS/"
            date_kind  = "aifs_date"
          },
          {
            gcs_prefix = "output/gencast/{aifs_date}/"
            local_dir  = "gencast/output/ethiopia/"
            date_kind  = "aifs_date"
          },
        ]
        git_push  = false
        date_kind = "aifs_date"
      }
    }
  }
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

  project_id  = var.project_id
  region      = var.region
  environment = local.environment
  regions     = local.regions

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

  regions = local.regions

  common_gcs_bucket     = module.storage.common_bucket_name
  region_buckets        = module.storage.region_bucket_names
  service_account_email = module.storage.pipeline_service_account_email

  # Prod: on-demand GPUs for reliability
  use_preemptible_gpu = false

  # Container images (pinned versions in prod)
  downloader_image     = "gcr.io/${var.project_id}/monsoon-downloader:v1.0.0"
  pipeline_state_image = "gcr.io/${var.project_id}/monsoon-pipeline-state:v1.0.0"
  postprocess_image    = "gcr.io/${var.project_id}/monsoon-postprocess:v1.0.0"
  blend_image          = "gcr.io/${var.project_id}/monsoon-blend:v1.0.0"
  sync_image           = "gcr.io/${var.project_id}/monsoon-sync:v1.0.0"
  aifs_image           = "gcr.io/${var.project_id}/monsoon-aifs:v1.0.0"
  neuralgcm_image      = "gcr.io/${var.project_id}/monsoon-neuralgcm:v1.0.0"
  gencast_image        = "gcr.io/${var.project_id}/monsoon-gencast:v1.0.0"

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

  regions           = local.regions
  full_field_models = ["aifs", "aifs_ens"]

  # Prod: frequent runs matching current HPC schedule
  pipeline_schedule = "*/15 * * * *" # Every 15 minutes (checks for new data)

  cloud_run_services     = module.compute.cloud_run_services
  pipeline_state_service = module.compute.pipeline_state_service
  batch_job_template     = module.compute.batch_job_template
  common_bucket          = module.storage.common_bucket_name
  region_buckets         = module.storage.region_bucket_names

  pipeline_service_account_id    = module.storage.pipeline_service_account_name
  pipeline_service_account_email = module.storage.pipeline_service_account_email

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

output "common_bucket" {
  description = "Common data bucket name"
  value       = module.storage.common_bucket_name
}

output "region_buckets" {
  description = "Per-region data bucket names"
  value       = module.storage.region_bucket_names
}

output "workflow_url" {
  description = "Cloud Workflow execution URL"
  value       = module.orchestration.workflow_url
}

output "monitoring_dashboard_url" {
  description = "Cloud Monitoring dashboard URL"
  value       = module.monitoring.dashboard_url
}
