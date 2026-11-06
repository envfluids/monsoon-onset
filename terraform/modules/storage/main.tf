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
      id     = "keep-most-recent-versions"
      action = "KEEP"
      most_recent_versions {
        keep_count = 3
      }
    }
  }

  # Images tagged with a DryWetCast role (products/drywetcast/pins.yaml) are
  # never deleted, so the fallback version survives any number of rebuilds.
  # KEEP takes precedence over DELETE.
  dynamic "cleanup_policies" {
    for_each = var.environment == "dev" ? [1] : []
    content {
      id     = "keep-role-tagged-images"
      action = "KEEP"
      condition {
        tag_state    = "TAGGED"
        tag_prefixes = ["live", "fallback", "candidate"]
      }
    }
  }

  dynamic "cleanup_policies" {
    for_each = var.environment == "dev" ? [1] : []
    content {
      id     = "delete-old-images-after-retention"
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

# pipeline-state reads Batch job states for the ensemble fallback ladder.
resource "google_project_iam_member" "pipeline_batch_job_viewer" {
  count = var.grant_pipeline_batch_job_viewer ? 1 : 0

  project = var.project_id
  role    = "roles/batch.jobsViewer"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# -----------------------------------------------------------------------------
# External Share Bucket (optional; created only when readers are configured)
# gs://${name_prefix}-${env}-share-${project_id}/
#   Read-only data for external collaborators, e.g. regional subsets of the
#   AIFS reforecasts and IMERG. Each reader gets its own service account with
#   objectViewer on this bucket only. Keys are created outside Terraform so
#   private keys never enter state.
# -----------------------------------------------------------------------------

locals {
  external_share_enabled = length(var.external_share_readers) > 0
}

resource "google_storage_bucket" "share" {
  count = local.external_share_enabled ? 1 : 0

  name     = "${var.name_prefix}-${var.environment}-share-${var.project_id}"
  project  = var.project_id
  location = var.region

  force_destroy = var.environment == "dev"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "external-share"
  }
}

resource "google_service_account" "share_reader" {
  for_each = var.external_share_readers

  project      = var.project_id
  account_id   = "ext-${each.key}-reader"
  display_name = each.value
}

# Each external reader can list and read objects in the share bucket only
resource "google_storage_bucket_iam_member" "share_readers" {
  for_each = google_service_account.share_reader

  bucket = google_storage_bucket.share[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value.email}"
}

# Uploaders can add, replace and delete objects in the share bucket only
resource "google_storage_bucket_iam_member" "share_uploaders" {
  for_each = local.external_share_enabled ? toset(var.external_share_uploaders) : toset([])

  bucket = google_storage_bucket.share[0].name
  role   = "roles/storage.objectAdmin"
  member = each.value
}
