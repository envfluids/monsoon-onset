# -----------------------------------------------------------------------------
# Compute Module
# Cloud Run services and Cloud Batch templates
# -----------------------------------------------------------------------------

locals {
  gpu_machine_type_defaults = {
    nvidia-tesla-a100 = "a2-highgpu-1g"
    nvidia-l4         = "g2-standard-8"
    nvidia-tesla-t4   = "n1-standard-8"
    nvidia-tesla-v100 = "n1-standard-8"
  }

  gpu_machine_type = (
    var.gpu_machine_type != ""
    ? var.gpu_machine_type
    : lookup(local.gpu_machine_type_defaults, var.gpu_type, "n1-standard-8")
  )

  cloud_run_services = {
    downloader = {
      name    = "${var.name_prefix}-${var.environment}-downloader"
      image   = var.downloader_image
      memory  = "4Gi"
      cpu     = "2"
      timeout = "900s" # 15 min for downloads
    }
    postprocess = {
      name    = "${var.name_prefix}-${var.environment}-postprocess"
      image   = var.postprocess_image
      memory  = "16Gi" # CDO/NCL can be memory intensive
      cpu     = "4"
      timeout = "1800s" # 30 min
    }
    blend = {
      name    = "${var.name_prefix}-${var.environment}-blend"
      image   = var.blend_image
      memory  = "8Gi"
      cpu     = "4"
      timeout = "1800s" # 30 min
    }
    sync = {
      name    = "${var.name_prefix}-${var.environment}-sync"
      image   = var.sync_image
      memory  = "1Gi"
      cpu     = "1"
      timeout = "600s" # 10 min
    }
  }
}

# -----------------------------------------------------------------------------
# Cloud Run Jobs (for lightweight pipeline stages)
# Using Jobs (not Services) since these are batch workloads
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

        # Common environment variables
        env {
          name  = "ENVIRONMENT"
          value = var.environment
        }
        env {
          name  = "GCS_BUCKET"
          value = var.gcs_bucket
        }
        env {
          name  = "GCS_WEIGHTS_BUCKET"
          value = var.weights_bucket
        }
        env {
          name  = "PROJECT_ID"
          value = var.project_id
        }

        # Region will be passed at execution time
        env {
          name  = "FORECAST_REGION"
          value = "india" # Default, overridden at runtime
        }
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
      template[0].template[0].containers[0].image, # Allow image updates outside TF
    ]
  }
}

# -----------------------------------------------------------------------------
# Cloud Run Service for lightweight source availability checks
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "ic_checker" {
  name                = "${var.name_prefix}-${var.environment}-ic-checker"
  project             = var.project_id
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment != "dev"

  template {
    service_account = var.service_account_email

    containers {
      image = var.ic_checker_image

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
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
  }

  labels = {
    environment = var.environment
    managed_by  = "terraform"
    component   = "ic-checker"
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image, # Allow image updates outside TF
    ]
  }
}
