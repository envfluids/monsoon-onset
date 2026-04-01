# -----------------------------------------------------------------------------
# Compute Module
# Cloud Run services, Cloud Batch templates, TPU configuration
# -----------------------------------------------------------------------------

locals {
  cloud_run_services = {
    downloader = {
      name   = "${var.name_prefix}-${var.environment}-downloader"
      image  = var.downloader_image
      memory = "2Gi"
      cpu    = "2"
      timeout = "900s"  # 15 min for downloads
    }
    postprocess = {
      name   = "${var.name_prefix}-${var.environment}-postprocess"
      image  = var.postprocess_image
      memory = "16Gi"  # CDO/NCL can be memory intensive
      cpu    = "4"
      timeout = "1800s"  # 30 min
    }
    blend = {
      name   = "${var.name_prefix}-${var.environment}-blend"
      image  = var.blend_image
      memory = "8Gi"
      cpu    = "4"
      timeout = "1800s"  # 30 min
    }
    sync = {
      name   = "${var.name_prefix}-${var.environment}-sync"
      image  = var.sync_image
      memory = "1Gi"
      cpu    = "1"
      timeout = "600s"  # 10 min
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
          value = "india"  # Default, overridden at runtime
        }
      }

      timeout     = each.value.timeout
      max_retries = 2

      service_account = var.service_account_email

      # VPC access for internal resources
      vpc_access {
        connector = var.vpc_connector_id
        egress    = "ALL_TRAFFIC"
      }
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
      template[0].template[0].containers[0].image,  # Allow image updates outside TF
    ]
  }
}

# -----------------------------------------------------------------------------
# TPU Configuration (for NeuralGCM JAX inference)
# TPU VMs are created on-demand by Cloud Workflows
# This defines the configuration template
# -----------------------------------------------------------------------------

# Service account for TPU workloads
resource "google_service_account" "tpu" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-${var.environment}-tpu"
  display_name = "Monsoon TPU Service Account (${var.environment})"
}

# TPU SA needs storage access
resource "google_project_iam_member" "tpu_storage" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.tpu.email}"
}

# TPU SA needs logging
resource "google_project_iam_member" "tpu_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.tpu.email}"
}

# Output TPU configuration for use by orchestration
locals {
  tpu_config = {
    service_account = google_service_account.tpu.email
    tpu_type        = var.tpu_type
    zone            = "${var.region}-a"  # TPUs require zone specification
    network         = var.vpc_id
    subnetwork      = var.vpc_subnetwork
    preemptible     = var.environment == "dev"

    # Startup script template
    startup_script = <<-EOF
      #!/bin/bash
      set -e

      # Install dependencies
      pip install gcsfs google-cloud-storage

      # Download and run the NeuralGCM inference
      gsutil cp gs://${var.gcs_bucket}/scripts/run_neuralgcm.py /tmp/
      python /tmp/run_neuralgcm.py \
        --region $${FORECAST_REGION} \
        --date $${FORECAST_DATE} \
        --bucket ${var.gcs_bucket}

      # Signal completion
      touch /tmp/done
    EOF
  }
}
