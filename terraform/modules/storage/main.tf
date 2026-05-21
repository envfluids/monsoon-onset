# -----------------------------------------------------------------------------
# Storage Module
# Creates GCS buckets, Artifact Registry, and lifecycle policies
#
# Buckets:
#   common: shared weights, ICs, full-field model outputs, intermediate markers
#   region (one per region): post-processed and blended region-specific outputs
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Common Bucket
# gs://${name_prefix}-${env}-common-${project_id}/
#   weights/    — model checkpoints, sparse matrices, blend supports, mask files
#   ic/         — initial conditions shared across regions
#   full_field/ — raw model outputs shared across regions
#   intermediate/ — markers, latest-date files, per-(model,region) done flags
# -----------------------------------------------------------------------------

resource "google_storage_bucket" "common" {
  name     = "${var.name_prefix}-${var.environment}-common-${var.project_id}"
  project  = var.project_id
  location = var.region

  force_destroy = var.environment == "dev"

  uniform_bucket_level_access = true

  versioning {
    enabled = var.enable_versioning
  }

  # Archive full-field raw forecasts to NEARLINE after archive_after_days
  dynamic "lifecycle_rule" {
    for_each = var.archive_after_days != null ? [1] : []
    content {
      condition {
        age                   = var.archive_after_days
        matches_storage_class = ["STANDARD"]
        matches_prefix        = ["full_field/"]
      }
      action {
        type          = "SetStorageClass"
        storage_class = "NEARLINE"
      }
    }
  }

  # Delete ic/, intermediate/, and JAX compilation cache objects after retention_days
  lifecycle_rule {
    condition {
      age            = var.retention_days
      matches_prefix = ["ic/", "intermediate/", "jax-cache/"]
    }
    action {
      type = "Delete"
    }
  }

  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }

  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "common"
  }
}

# -----------------------------------------------------------------------------
# Per-Region Buckets
# gs://${name_prefix}-${env}-region-${region}-${project_id}/
#   output/  — post-processed and blended outputs for this region
#   latest.txt — last successfully synced date
# -----------------------------------------------------------------------------

resource "google_storage_bucket" "region" {
  for_each = var.regions

  name     = "${var.name_prefix}-${var.environment}-${each.key}-${var.project_id}"
  project  = var.project_id
  location = var.region

  force_destroy = var.environment == "dev"

  uniform_bucket_level_access = true

  versioning {
    enabled = var.enable_versioning
  }

  dynamic "lifecycle_rule" {
    for_each = var.archive_after_days != null ? [1] : []
    content {
      condition {
        age                   = var.archive_after_days
        matches_storage_class = ["STANDARD"]
        matches_prefix        = ["output/"]
      }
      action {
        type          = "SetStorageClass"
        storage_class = "NEARLINE"
      }
    }
  }

  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }

  labels = {
    environment     = var.environment
    managed_by      = "terraform"
    forecast_region = each.key
  }
}

# -----------------------------------------------------------------------------
# Folder markers (placeholder objects to keep top-level prefixes visible)
# -----------------------------------------------------------------------------

resource "google_storage_bucket_object" "common_folders" {
  for_each = toset([
    "weights/.keep",
    "ic/.keep",
    "full_field/.keep",
    "intermediate/.keep",
    "jax-cache/.keep",
  ])

  bucket  = google_storage_bucket.common.name
  name    = each.key
  content = "# Placeholder for folder structure"
}

resource "google_storage_bucket_object" "region_folders" {
  for_each = google_storage_bucket.region

  bucket  = each.value.name
  name    = "output/.keep"
  content = "# Placeholder for folder structure"
}

# -----------------------------------------------------------------------------
# Artifact Registry for Container Images
# -----------------------------------------------------------------------------

resource "google_artifact_registry_repository" "containers" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.name_prefix}-${var.environment}-containers"
  format        = "DOCKER"

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }

  dynamic "cleanup_policies" {
    for_each = var.environment == "dev" ? [1] : []
    content {
      id     = "delete-old-images"
      action = "DELETE"
      condition {
        older_than = var.artifact_registry_cleanup_older_than
      }
    }
  }
}

# -----------------------------------------------------------------------------
# IAM Bindings
# -----------------------------------------------------------------------------

resource "google_service_account" "pipeline" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-${var.environment}-pipeline"
  display_name = "Monsoon Pipeline Service Account (${var.environment})"
}

# Pipeline SA can read/write common bucket (ICs, weights, full-field, intermediate)
resource "google_storage_bucket_iam_member" "pipeline_common_bucket" {
  bucket = google_storage_bucket.common.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

# Pipeline SA can read/write each region bucket (post-processed outputs, blend, sync)
resource "google_storage_bucket_iam_member" "pipeline_region_buckets" {
  for_each = google_storage_bucket.region

  bucket = each.value.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_artifact_registry_repository_iam_member" "pipeline_ar" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.containers.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_batch_agent_reporter" {
  project = var.project_id
  role    = "roles/batch.agentReporter"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}
