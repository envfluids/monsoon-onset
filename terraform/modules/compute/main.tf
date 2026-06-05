# -----------------------------------------------------------------------------
# Compute Module
# Cloud Run jobs and Cloud Batch templates
# -----------------------------------------------------------------------------

locals {
  gpu_machine_type_defaults = {
    nvidia-tesla-a100 = "a2-highgpu-1g"
    nvidia-l4         = "g2-standard-16"
    nvidia-tesla-t4   = "n1-standard-8"
    nvidia-tesla-v100 = "n1-standard-8"
  }

  gpu_machine_type = (
    var.gpu_machine_type != ""
    ? var.gpu_machine_type
    : lookup(local.gpu_machine_type_defaults, var.gpu_type, "n1-standard-8")
  )

  gpu_machine_resource_defaults = {
    "a2-highgpu-1g" = {
      cpu_milli  = 12000
      memory_mib = 87040
      gpu_type   = "nvidia-tesla-a100"
      gpu_count  = 1
    }
    "a2-highgpu-2g" = {
      cpu_milli  = 24000
      memory_mib = 174080
      gpu_type   = "nvidia-tesla-a100"
      gpu_count  = 2
    }
    "a2-highgpu-4g" = {
      cpu_milli  = 48000
      memory_mib = 348160
      gpu_type   = "nvidia-tesla-a100"
      gpu_count  = 4
    }
    "a2-highgpu-8g" = {
      cpu_milli  = 96000
      memory_mib = 696320
      gpu_type   = "nvidia-tesla-a100"
      gpu_count  = 8
    }
    "g2-standard-16" = {
      cpu_milli  = 16000
      memory_mib = 65536
      gpu_type   = "nvidia-l4"
      gpu_count  = 1
    }
    "n1-standard-8" = {
      cpu_milli  = 8000
      memory_mib = 30720
      gpu_type   = var.gpu_type
      gpu_count  = 1
    }
  }

  default_batch_model_resources = {
    AIFS_single_v2 = {
      machine_type        = local.gpu_machine_type
      boot_disk_size_gb   = var.batch_boot_disk_size_gb
      boot_disk_type      = var.batch_boot_disk_type
      install_gpu_drivers = true
      max_run_duration    = "1800s"
      mount_common_bucket = true
    }
    neuralgcm = {
      machine_type        = local.gpu_machine_type
      boot_disk_size_gb   = var.batch_boot_disk_size_gb
      boot_disk_type      = var.batch_boot_disk_type
      install_gpu_drivers = true
      max_run_duration    = "3600s"
      mount_common_bucket = true
    }
    blend = {
      machine_type        = "e2-highmem-4"
      boot_disk_size_gb   = var.batch_boot_disk_size_gb
      boot_disk_type      = var.batch_boot_disk_type
      cpu_milli           = 4000
      memory_mib          = 32768
      gpu_type            = null
      gpu_count           = null
      install_gpu_drivers = false
      max_run_duration    = "3600s"
      mount_common_bucket = true
      provisioning_model  = "STANDARD"
    }
    diagnostics = {
      machine_type        = "e2-highmem-4"
      boot_disk_size_gb   = var.batch_boot_disk_size_gb
      boot_disk_type      = var.batch_boot_disk_type
      cpu_milli           = 4000
      memory_mib          = 32768
      gpu_type            = null
      gpu_count           = null
      install_gpu_drivers = false
      max_run_duration    = "7200s"
      mount_common_bucket = true
    }
  }

  batch_model_resource_configs = merge(local.default_batch_model_resources, var.batch_model_resources)

  batch_model_resources = {
    for model, config in local.batch_model_resource_configs : model => {
      machine_type      = coalesce(try(config.machine_type, null), local.gpu_machine_type)
      boot_disk_size_gb = coalesce(try(config.boot_disk_size_gb, null), var.batch_boot_disk_size_gb)
      boot_disk_type    = coalesce(try(config.boot_disk_type, null), var.batch_boot_disk_type, "pd-balanced")
      cpu_milli = coalesce(
        try(config.cpu_milli, null),
        try(local.gpu_machine_resource_defaults[coalesce(try(config.machine_type, null), local.gpu_machine_type)].cpu_milli, null)
      )
      memory_mib = coalesce(
        try(config.memory_mib, null),
        try(local.gpu_machine_resource_defaults[coalesce(try(config.machine_type, null), local.gpu_machine_type)].memory_mib, null)
      )
      gpu_type = coalesce(try(config.install_gpu_drivers, null), true) ? coalesce(
        try(config.gpu_type, null),
        try(local.gpu_machine_resource_defaults[coalesce(try(config.machine_type, null), local.gpu_machine_type)].gpu_type, null)
      ) : null
      gpu_count = coalesce(try(config.install_gpu_drivers, null), true) ? coalesce(
        try(config.gpu_count, null),
        try(local.gpu_machine_resource_defaults[coalesce(try(config.machine_type, null), local.gpu_machine_type)].gpu_count, null)
      ) : null
      install_gpu_drivers = coalesce(
        try(config.install_gpu_drivers, null),
        coalesce(
          try(config.gpu_count, null),
          try(local.gpu_machine_resource_defaults[coalesce(try(config.machine_type, null), local.gpu_machine_type)].gpu_count, null),
          0,
        ) > 0
      )
      max_run_duration = coalesce(
        try(config.max_run_duration, null),
        model == "AIFS_ENS_v2" ? "7200s" : "1800s"
      )
      mount_common_bucket = coalesce(
        try(config.mount_common_bucket, null),
        false
      )
      provisioning_model = coalesce(
        try(config.provisioning_model, null),
        var.use_preemptible_gpu ? "SPOT" : "STANDARD"
      )
    }
  }

  # JSON-encoded summary of {region: models} consumed by pipeline-state.
  region_models = jsonencode({ for k, v in var.regions : k => v.models })

  # Full regions map, JSON-encoded — pipeline-state needs the entire object.
  regions_json = jsonencode(var.regions)

  region_buckets_json = jsonencode(var.region_buckets)

  cloud_run_job_env = {
    sync = {
      ENABLE_DRIVE    = "true"
      MONSOON_CLUSTER = var.environment == "dev" ? "gcp-dev" : "gcp"
    }
  }

  cloud_run_services = {
    downloader = {
      name    = "${var.name_prefix}-${var.environment}-downloader"
      image   = var.downloader_image
      memory  = "4Gi"
      cpu     = "2"
      timeout = "900s"
      retries = 2
      # Optional env-var names to mount from Secret Manager when supplied in
      # var.external_api_secrets.
      secrets = ["ECMWF_API_KEY", "ECMWF_API_URL", "ECMWF_API_EMAIL"]
    }
    sync = {
      name    = "${var.name_prefix}-${var.environment}-sync"
      image   = var.sync_image
      memory  = "1Gi"
      cpu     = "1"
      timeout = "600s"
      retries = 2
      secrets = ["GOOGLE_DRIVE_CREDENTIALS_JSON", "GOOGLE_DRIVE_TOKEN_JSON"]
    }
    "tpu-dispatch" = {
      name    = "${var.name_prefix}-${var.environment}-tpu-dispatch"
      image   = var.tpu_dispatch_image
      memory  = "1Gi"
      cpu     = "1"
      timeout = "86400s"
      retries = 0
      secrets = []
    }
  }

  # Env-var name → Secret Manager secret_id (lower-kebab-case).
  # Keys are non-sensitive (only the values in var.external_api_secrets are);
  # nonsensitive() unwraps the keyset so it can be used as for_each below.
  external_secret_names = nonsensitive(toset(keys(var.external_api_secrets)))

  secret_id_for_env = {
    for name in local.external_secret_names : name => lower(replace(name, "_", "-"))
  }

  cloud_run_service_secret_ids = {
    for name, service in local.cloud_run_services :
    name => {
      for secret_name in service.secrets :
      secret_name => lookup(local.secret_id_for_env, secret_name, null)
      if contains(local.external_secret_names, secret_name)
    }
  }
}

# -----------------------------------------------------------------------------
# Secret Manager — one secret + version per entry in external_api_secrets.
# Values come from a sensitive terraform variable; only the digest of the
# version lives in state.
# -----------------------------------------------------------------------------

resource "google_secret_manager_secret" "external_api" {
  for_each = local.external_secret_names

  project   = var.project_id
  secret_id = local.secret_id_for_env[each.key]

  # Org policy `constraints/gcp.resourceLocations` blocks `global` replication;
  # pin to the same region as the rest of the pipeline.
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "external_api" {
  for_each = local.external_secret_names

  secret      = google_secret_manager_secret.external_api[each.key].id
  secret_data = var.external_api_secrets[each.key]
}

resource "google_secret_manager_secret_iam_member" "pipeline_secret_accessor" {
  for_each = google_secret_manager_secret.external_api

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_email}"
}

# -----------------------------------------------------------------------------
# Cloud Run Jobs (lightweight pipeline stages)
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "pipeline_jobs" {
  for_each = local.cloud_run_services

  name                = each.value.name
  project             = var.project_id
  location            = var.region
  deletion_protection = var.environment != "dev"

  # Ensure secrets, versions, and IAM bindings exist before the job revision
  # tries to mount them — Cloud Run validates secret access at revision-create
  # time and rejects the update if IAM hasn't propagated yet.
  depends_on = [
    google_secret_manager_secret_version.external_api,
    google_secret_manager_secret_iam_member.pipeline_secret_accessor,
  ]

  template {
    template {
      containers {
        image = each.value.image

        resources {
          limits = {
            memory = each.value.memory
            cpu    = each.value.cpu
          }
        }

        env {
          name  = "ENVIRONMENT"
          value = var.environment
        }
        env {
          name  = "GCS_COMMON_BUCKET"
          value = var.common_gcs_bucket
        }
        env {
          name  = "GCS_REGION_BUCKETS"
          value = local.region_buckets_json
        }
        env {
          name  = "REGIONS"
          value = local.regions_json
        }
        env {
          name  = "REGION_MODELS"
          value = local.region_models
        }
        env {
          name  = "PROJECT_ID"
          value = var.project_id
        }

        dynamic "env" {
          for_each = lookup(local.cloud_run_job_env, each.key, {})
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.cloud_run_service_secret_ids[each.key]
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }

        # FORECAST_REGION, FORECAST_REGIONS, SYNC_SPEC, DATE, etc. are set per-execution
        # by the workflow via containerOverrides — no default here.
      }

      timeout     = each.value.timeout
      max_retries = each.value.retries

      service_account = var.service_account_email

    }

    task_count = 1

    # Force a new revision when any mounted secret version or its IAM grant
    # changes. Without this Cloud Run never re-evaluates the Ready condition
    # because the job spec itself is unchanged.
    labels = {
      secret-deps-hash = sha1(jsonencode([
        for env_name, secret_id in local.cloud_run_service_secret_ids[each.key] : {
          version_name = try(
            google_secret_manager_secret_version.external_api[env_name].name,
            ""
          )
          iam_etag = try(
            google_secret_manager_secret_iam_member.pipeline_secret_accessor[env_name].etag,
            ""
          )
        }
      ]))
    }
  }

  labels = {
    environment = var.environment
    managed_by  = "terraform"
    component   = each.key
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_project_iam_member" "pipeline_tpu_admin" {
  project = var.project_id
  role    = "roles/tpu.admin"
  member  = "serviceAccount:${var.service_account_email}"
}

resource "google_service_account_iam_member" "pipeline_tpu_vm_service_account_user" {
  service_account_id = var.service_account_id
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.service_account_email}"
}

# -----------------------------------------------------------------------------
# Cloud Run Service for full-pipeline state inspection
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "pipeline_state" {
  name                = "${var.name_prefix}-${var.environment}-pipeline-state"
  project             = var.project_id
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment != "dev"

  template {
    service_account = var.service_account_email

    containers {
      image = var.pipeline_state_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          memory = "512Mi"
          cpu    = "1"
        }
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "GCS_COMMON_BUCKET"
        value = var.common_gcs_bucket
      }
      env {
        name  = "GCS_REGION_BUCKETS"
        value = local.region_buckets_json
      }
      env {
        name  = "REGIONS"
        value = local.regions_json
      }
      env {
        name  = "REGION_MODELS"
        value = local.region_models
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
  }

  labels = {
    environment = var.environment
    managed_by  = "terraform"
    component   = "pipeline-state"
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}
