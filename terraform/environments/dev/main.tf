# -----------------------------------------------------------------------------
# Development Environment
# -----------------------------------------------------------------------------

terraform {
  backend "gcs" {
    # Configure via: terraform init -backend-config="bucket=<your-tf-state-bucket>"
    prefix = "monsoon/dev"
  }
}

# Reconciles the ecmwf-api-url secret version that exists in GCP but was
# missing from state. No-op after the first successful apply imports it.
import {
  to = module.compute.google_secret_manager_secret_version.external_api["ECMWF_API_URL"]
  id = "projects/${var.project_id}/secrets/ecmwf-api-url/versions/1"
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

variable "google_drive_credentials_json" {
  description = "Google Drive OAuth credentials JSON for the sync job. Prefer passing from sync/.auth/credentials.json via a gitignored tfvars file."
  type        = string
  default     = ""
  sensitive   = true
}

variable "google_drive_token_json" {
  description = "Google Drive OAuth token JSON for the sync job. Prefer passing from sync/.auth/token.json via a gitignored tfvars file."
  type        = string
  default     = ""
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
  description = "Versioned model names to disable globally in this environment. Hyphen and underscore spellings are both accepted, for example AIFS-ENS-v2 or AIFS_ENS_v2."
  type        = set(string)
  default     = ["gencast", "AIFS_single_v2", "neuralgcm"]

  validation {
    condition = alltrue([
      for model in var.disabled_models :
      contains(["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm", "gencast"], replace(model, "-", "_"))
    ])
    error_message = "disabled_models entries must be one of AIFS_single_v2, AIFS_ENS_v2, neuralgcm, gencast (case-sensitive)."
  }
}

variable "disabled_region_models" {
  description = "Per-region models to switch off without removing them from the region definition, for seasonal on/off control. Map of region → versioned model names; hyphen and underscore spellings are both accepted."
  type        = map(set(string))
  default = {
    ethiopia = ["AIFS_ENS_v2"]
  }

  validation {
    condition = alltrue([
      for region, models in var.disabled_region_models :
      contains(["india", "ethiopia"], region) && alltrue([
        for model in models :
        contains(["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm", "gencast"], replace(model, "-", "_"))
      ])
    ])
    error_message = "disabled_region_models keys must be india or ethiopia, and models must be one of AIFS_single_v2, AIFS_ENS_v2, neuralgcm, gencast (case-sensitive)."
  }
}

variable "disabled_stages" {
  description = "Per-region stages to switch off without removing them from the region definition, for seasonal on/off control. Map of region → stages (blend, model_diagnostics, drywetcast, sync)."
  type        = map(set(string))
  default = {
    india    = ["model_diagnostics"]
    ethiopia = ["blend", "model_diagnostics"]
  }

  validation {
    condition = alltrue([
      for region, stages in var.disabled_stages :
      contains(["india", "ethiopia"], region) && alltrue([
        for stage in stages : contains(["blend", "model_diagnostics", "drywetcast", "sync"], stage)
      ])
    ])
    error_message = "disabled_stages keys must be india or ethiopia, and stages must be blend, model_diagnostics, drywetcast or sync."
  }
}

variable "drywetcast_ncmrwf_cutoff_utc" {
  description = "UTC time (HH:MM) on the forecast date after which a pass makes one last NCMRWF check and marks missing NCMRWF DryWetCast products unavailable. Needs a scheduled pass at or after it."
  type        = string
  default     = "14:00"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.drywetcast_ncmrwf_cutoff_utc))
    error_message = "drywetcast_ncmrwf_cutoff_utc must be HH:MM (UTC)."
  }
}

variable "scheduler_paused" {
  description = "Pause the pipeline Cloud Scheduler job. Workflow runs can still be started manually."
  type        = bool
  default     = false
}

locals {
  environment        = "dev"
  gencast_tpu_region = regex("^(.+)-[a-z]$", var.gencast_tpu_zone)[0]
  disabled_model_ids = toset([for model in var.disabled_models : replace(model, "-", "_")])
  drive_api_secrets = {
    for name, value in {
      GOOGLE_DRIVE_CREDENTIALS_JSON = var.google_drive_credentials_json
      GOOGLE_DRIVE_TOKEN_JSON       = var.google_drive_token_json
    } : name => value
    if value != ""
  }
  external_api_secrets = merge(var.external_api_secrets, local.drive_api_secrets)
  model_sync_rule_exclusions = {
    AIFS_single_v2 = {
      india    = ["AIFS_single_v2"]
      ethiopia = ["AIFS_single_v2"]
    }
    AIFS_ENS_v2 = {
      ethiopia = ["AIFS_ENS_v2"]
    }
    neuralgcm = {
      india    = ["NeuralGCM"]
      ethiopia = ["NeuralGCM"]
    }
    gencast = {
      ethiopia = ["GenCast"]
    }
  }
  model_stage_exclusions = {}
  additional_subnets = local.gencast_tpu_region == var.region ? {} : {
    gencast-tpu = {
      region = local.gencast_tpu_region
      cidr   = var.gencast_tpu_subnet_cidr
    }
  }

  base_regions = {
    india = {
      models = ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm"]
      stages = ["model_diagnostics", "drywetcast", "sync"]
      sync = {
        rules     = ["AIFS_single_v2", "NeuralGCM", "model_diagnostics"]
        git_push  = true
        date_kind = "date"
      }
    }
    ethiopia = {
      models = ["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm", "gencast"]
      stages = ["blend", "model_diagnostics", "sync"]
      sync = {
        rules     = ["AIFS_single_v2", "AIFS_ENS_v2", "NeuralGCM", "GenCast", "blend", "model_diagnostics"]
        git_push  = false
        date_kind = "aifs_date"
      }
    }
  }

  # Models off in each region: disabled everywhere, or only in that region.
  disabled_model_ids_by_region = {
    for region_name in keys(local.base_regions) :
    region_name => setunion(
      local.disabled_model_ids,
      [for model in try(var.disabled_region_models[region_name], toset([])) : replace(model, "-", "_")],
    )
  }

  disabled_stages_by_region = {
    for region_name in keys(local.base_regions) :
    region_name => toset(flatten([
      [
        for model in local.disabled_model_ids_by_region[region_name] :
        lookup(lookup(local.model_stage_exclusions, model, {}), region_name, [])
      ],
      tolist(try(var.disabled_stages[region_name], toset([]))),
    ]))
  }

  # Disabling the blend or model_diagnostics stage also drops its same-named sync rule.
  disabled_sync_rules_by_region = {
    for region_name in keys(local.base_regions) :
    region_name => toset(flatten([
      [
        for model in local.disabled_model_ids_by_region[region_name] :
        lookup(lookup(local.model_sync_rule_exclusions, model, {}), region_name, [])
      ],
      tolist(try(var.disabled_stages[region_name], toset([]))),
    ]))
  }

  regions = {
    for region_name, cfg in local.base_regions :
    region_name => {
      models = [
        for model in cfg.models : model
        if !contains(local.disabled_model_ids_by_region[region_name], model)
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
  service_account_id    = module.storage.pipeline_service_account_name

  external_api_secrets = local.external_api_secrets

  # Dev: use spot GPUs for model Batch jobs
  use_preemptible_gpu = true
  gencast_tpu_zone    = var.gencast_tpu_zone
  tpu_vpc_subnetwork  = module.networking.subnetwork_ids_by_region[local.gencast_tpu_region]

  # Replaces the module default map: AIFS_ENS_v2 keeps its module-default
  # settings but runs on STANDARD (non-spot) VMs instead of the dev SPOT default.
  batch_model_resources = {
    AIFS_ENS_v2 = {
      machine_type        = "a2-highgpu-4g"
      boot_disk_size_gb   = 300
      cpu_milli           = 12000
      memory_mib          = 204800
      install_gpu_drivers = true
      max_run_duration    = "7200s"
      mount_common_bucket = true
      gcs_mount_options = [
        "--implicit-dirs",
        "--metadata-cache-negative-ttl-secs=0",
        "--profile=aiml-checkpointing",
      ]
      provisioning_model = "STANDARD"
    }
  }

  # Container images — pulled from Artifact Registry created by storage module
  downloader_image     = "${module.storage.artifact_registry_url}/monsoon-downloader:latest"
  pipeline_state_image = "${module.storage.artifact_registry_url}/monsoon-pipeline-state:latest"
  blend_image          = "${module.storage.artifact_registry_url}/monsoon-blend:latest"
  sync_image           = "${module.storage.artifact_registry_url}/monsoon-sync:latest"
  aifs_v2_image        = "${module.storage.artifact_registry_url}/monsoon-aifs-v2:latest"
  aifs_ens_v2_image    = "${module.storage.artifact_registry_url}/monsoon-aifs-ens-v2:latest"
  neuralgcm_image      = "${module.storage.artifact_registry_url}/monsoon-neuralgcm:latest"
  gencast_image        = "${module.storage.artifact_registry_url}/monsoon-gencast:latest"
  tpu_dispatch_image   = "${module.storage.artifact_registry_url}/monsoon-tpu-dispatch:latest"
  drywetcast_image     = "${module.storage.artifact_registry_url}/monsoon-drywetcast:latest"

  # NCMRWF key: the existing ncmrwf-api-key secret, referenced (not managed) by the job.
  drywetcast_ncmrwf_secret_id = "ncmrwf-api-key"
  pipeline_state_env = {
    DRYWETCAST_NCMRWF_CUTOFF_UTC = var.drywetcast_ncmrwf_cutoff_utc
  }

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
  full_field_models = setsubtract(toset(["AIFS_single_v2", "AIFS_ENS_v2", "neuralgcm"]), local.disabled_model_ids)

  # Extra Batch environment per model; read by run_model_ENS.py (PR #11).
  batch_model_env = {
    AIFS_ENS_v2 = {
      AIFS_ENS_N_MEMBERS       = "51"
      AIFS_ENS_LEAD_TIME_HOURS = "168"
    }
  }

  # Dev: less frequent runs
  pipeline_schedule       = "0 8,10,14 * * *" # 08:00, 10:00 and 14:00 UTC, daily
  scheduler_paused        = var.scheduler_paused
  call_log_level          = "LOG_ALL_CALLS"
  execution_history_level = "EXECUTION_HISTORY_DETAILED"

  cloud_run_services            = module.compute.cloud_run_services
  pipeline_state_service_name   = module.compute.pipeline_state_service_name
  pipeline_state_url            = module.compute.pipeline_state_url
  batch_job_template            = module.compute.batch_job_template
  gencast_tpu_dispatch_template = module.compute.gencast_tpu_dispatch_template
  common_bucket                 = module.storage.common_bucket_name
  region_buckets                = module.storage.region_bucket_names

  pipeline_service_account_id    = module.storage.pipeline_service_account_name
  pipeline_service_account_email = module.storage.pipeline_service_account_email
}

# -----------------------------------------------------------------------------
# Cloud Build default service account — needs Cloud Run patch rights so the
# build's per-image deploy steps in cloudbuild.yaml can roll services/jobs
# to the just-pushed digest.
# -----------------------------------------------------------------------------

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  cloudbuild_default_sa = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_project_iam_member" "cloudbuild_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = local.cloudbuild_default_sa
}

resource "google_project_iam_member" "cloudbuild_service_account_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = local.cloudbuild_default_sa
}

# -----------------------------------------------------------------------------
# Monitoring
# -----------------------------------------------------------------------------

module "monitoring" {
  source = "../../modules/monitoring"

  project_id  = var.project_id
  region      = var.region
  environment = local.environment

  enable_alerts       = true
  notification_emails = ["zachary.freitag.johnson7@gmail.com"]

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
