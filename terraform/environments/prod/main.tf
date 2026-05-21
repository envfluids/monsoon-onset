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

variable "external_api_secrets" {
  description = "Map of env-var name → secret value for external APIs (e.g., ECMWF MARS). Pass via TF_VAR_external_api_secrets or a gitignored *.tfvars file."
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "gencast_tpu_zone" {
  description = "Zone for GenCast TPU v5p jobs. Defaults to us-central1-a; set to us-east5-a if TPU capacity is moved east."
  type        = string
  default     = "us-central1-a"

  validation {
    condition     = contains(["us-central1-a", "us-east5-a"], var.gencast_tpu_zone)
    error_message = "GenCast TPU v5p jobs should use us-central1-a or us-east5-a."
  }
}

variable "gencast_tpu_subnet_cidr" {
  description = "CIDR for the optional GenCast TPU subnet when gencast_tpu_zone is outside the primary region"
  type        = string
  default     = "10.16.0.0/20"
}

variable "disabled_models" {
  description = "Model names to disable globally in this environment. Hyphen and underscore spellings are both accepted, for example aifs-ens or aifs_ens."
  type        = set(string)
  default     = ["aifs-ens"]
}

locals {
  environment        = "prod"
  gencast_tpu_region = regex("^(.+)-[a-z]$", var.gencast_tpu_zone)[0]
  disabled_model_ids = toset([for model in var.disabled_models : replace(model, "-", "_")])
  model_sync_rule_exclusions = {
    aifs_ens = {
      ethiopia = ["AIFS_ENS", "blend"]
    }
  }
  model_stage_exclusions = {
    # The Ethiopia blend currently requires AIFS + AIFS-ENS.
    aifs_ens = {
      ethiopia = ["blend"]
    }
  }
  additional_subnets = local.gencast_tpu_region == var.region ? {} : {
    gencast-tpu = {
      region = local.gencast_tpu_region
      cidr   = var.gencast_tpu_subnet_cidr
    }
  }

  base_regions = {
    india = {
      models = ["aifs", "neuralgcm"]
      stages = ["blend", "sync"]
      sync = {
        rules     = ["blend_google"]
        git_push  = true
        date_kind = "date"
      }
    }
    ethiopia = {
      models = ["aifs", "aifs_ens", "gencast"]
      stages = ["blend", "sync"]
      sync = {
        rules     = ["AIFS", "AIFS_ENS", "GenCast", "blend"]
        git_push  = false
        date_kind = "aifs_date"
      }
    }
  }

  disabled_stages_by_region = {
    for region_name in keys(local.base_regions) :
    region_name => toset(flatten([
      for model in local.disabled_model_ids :
      lookup(lookup(local.model_stage_exclusions, model, {}), region_name, [])
    ]))
  }

  disabled_sync_rules_by_region = {
    for region_name in keys(local.base_regions) :
    region_name => toset(flatten([
      for model in local.disabled_model_ids :
      lookup(lookup(local.model_sync_rule_exclusions, model, {}), region_name, [])
    ]))
  }

  regions = {
    for region_name, cfg in local.base_regions :
    region_name => {
      models = [
        for model in cfg.models : model
        if !contains(local.disabled_model_ids, model)
      ]
      stages = [
        for stage in cfg.stages : stage
        if !contains(local.disabled_stages_by_region[region_name], stage)
      ]
      sync = {
        rules = [
          for rule in cfg.sync.rules : rule
          if !contains(local.disabled_sync_rules_by_region[region_name], rule)
        ]
        git_push  = cfg.sync.git_push
        date_kind = cfg.sync.date_kind
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

  additional_subnets = local.additional_subnets
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
  service_account_id    = module.storage.pipeline_service_account_name

  external_api_secrets = var.external_api_secrets

  # Prod: on-demand GPUs for reliability
  use_preemptible_gpu = false
  gencast_tpu_zone    = var.gencast_tpu_zone
  tpu_vpc_subnetwork  = module.networking.subnetwork_ids_by_region[local.gencast_tpu_region]

  # Container images — pulled from Artifact Registry created by storage module
  downloader_image     = "${module.storage.artifact_registry_url}/monsoon-downloader:latest"
  pipeline_state_image = "${module.storage.artifact_registry_url}/monsoon-pipeline-state:latest"
  blend_image          = "${module.storage.artifact_registry_url}/monsoon-blend:latest"
  sync_image           = "${module.storage.artifact_registry_url}/monsoon-sync:latest"
  aifs_image           = "${module.storage.artifact_registry_url}/monsoon-aifs:latest"
  neuralgcm_image      = "${module.storage.artifact_registry_url}/monsoon-neuralgcm:latest"
  gencast_image        = "${module.storage.artifact_registry_url}/monsoon-gencast:latest"
  tpu_dispatch_image   = "${module.storage.artifact_registry_url}/monsoon-tpu-dispatch:latest"

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
  full_field_models = setsubtract(toset(["aifs", "aifs_ens"]), local.disabled_model_ids)

  # Prod: frequent runs matching current HPC schedule
  pipeline_schedule = "*/15 * * * *" # Every 15 minutes (checks for new data)

  cloud_run_services            = module.compute.cloud_run_services
  pipeline_state_service_name   = module.compute.pipeline_state_service_name
  pipeline_state_url            = module.compute.pipeline_state_url
  batch_job_template            = module.compute.batch_job_template
  gencast_tpu_dispatch_template = module.compute.gencast_tpu_dispatch_template
  common_bucket                 = module.storage.common_bucket_name
  region_buckets                = module.storage.region_bucket_names

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
