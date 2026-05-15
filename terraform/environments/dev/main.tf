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

variable "external_api_secrets" {
  description = "Map of env-var name → secret value for external APIs (e.g., ECMWF MARS). Pass via TF_VAR_external_api_secrets or a gitignored *.tfvars file."
  type        = map(string)
  default     = {}
  sensitive   = true
}

locals {
  environment = "dev"

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

  # Dev: shorter retention, no archival
  retention_days     = 30
  enable_versioning  = false
  archive_after_days = null
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

  external_api_secrets = var.external_api_secrets

  # Dev: use spot GPUs for model Batch jobs
  use_preemptible_gpu = true

  # Container images — pulled from Artifact Registry created by storage module
  downloader_image     = "${module.storage.artifact_registry_url}/monsoon-downloader:latest"
  pipeline_state_image = "${module.storage.artifact_registry_url}/monsoon-pipeline-state:latest"
  postprocess_image    = "${module.storage.artifact_registry_url}/monsoon-postprocess:latest"
  blend_image          = "${module.storage.artifact_registry_url}/monsoon-blend:latest"
  sync_image           = "${module.storage.artifact_registry_url}/monsoon-sync:latest"
  aifs_image           = "${module.storage.artifact_registry_url}/monsoon-aifs:latest"
  neuralgcm_image      = "${module.storage.artifact_registry_url}/monsoon-neuralgcm:latest"
  gencast_image        = "${module.storage.artifact_registry_url}/monsoon-gencast:latest"

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

  # Dev: less frequent runs
  pipeline_schedule       = "0 */6 * * *" # Every 6 hours
  call_log_level          = "LOG_ALL_CALLS"
  execution_history_level = "EXECUTION_HISTORY_DETAILED"

  cloud_run_services          = module.compute.cloud_run_services
  pipeline_state_service_name = module.compute.pipeline_state_service_name
  pipeline_state_url          = module.compute.pipeline_state_url
  batch_job_template          = module.compute.batch_job_template
  common_bucket               = module.storage.common_bucket_name
  region_buckets              = module.storage.region_bucket_names

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
