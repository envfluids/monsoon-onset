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

  # JSON-encoded summary of {region: models} consumed by postprocess and pipeline-state.
  region_models = jsonencode({ for k, v in var.regions : k => v.models })

  # Full regions map, JSON-encoded — pipeline-state needs the entire object.
  regions_json = jsonencode(var.regions)

  region_buckets_json = jsonencode(var.region_buckets)

  cloud_run_services = {
    downloader = {
      name    = "${var.name_prefix}-${var.environment}-downloader"
      image   = var.downloader_image
      memory  = "4Gi"
      cpu     = "2"
      timeout = "900s"
    }
    postprocess = {
      name    = "${var.name_prefix}-${var.environment}-postprocess"
      image   = var.postprocess_image
      memory  = "16Gi"
      cpu     = "4"
      timeout = "1800s"
    }
    blend = {
      name    = "${var.name_prefix}-${var.environment}-blend"
      image   = var.blend_image
      memory  = "8Gi"
      cpu     = "4"
      timeout = "1800s"
    }
    sync = {
      name    = "${var.name_prefix}-${var.environment}-sync"
      image   = var.sync_image
      memory  = "1Gi"
      cpu     = "1"
      timeout = "600s"
    }
  }
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

        # FORECAST_REGION, FORECAST_REGIONS, SYNC_SPEC, DATE, etc. are set per-execution
        # by the workflow via containerOverrides — no default here.
      }

      timeout     = each.value.timeout
      max_retries = 2

      service_account = var.service_account_email
    }

    task_count = 1
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
