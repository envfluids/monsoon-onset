# -----------------------------------------------------------------------------
# Storage Module
# Creates GCS buckets, Artifact Registry, and lifecycle policies
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Main Data Bucket
# Structure: gs://monsoon-{env}/{region}/config|weights|support|output/
# -----------------------------------------------------------------------------

resource "google_storage_bucket" "main" {
  name     = "${var.name_prefix}-${var.environment}-data-${var.project_id}"
  project  = var.project_id
  location = var.region

  # Prevent accidental deletion in prod
  force_destroy = var.environment == "dev"

  uniform_bucket_level_access = true

  versioning {
    enabled = var.enable_versioning
  }

  # Lifecycle rules
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

  lifecycle_rule {
    condition {
      age            = var.retention_days
      matches_prefix = ["raw/", "intermediate/"]
    }
    action {
      type = "Delete"
    }
  }

  # CORS for potential web access
  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Create folder structure for each forecast region
# Using empty objects as folder markers
# -----------------------------------------------------------------------------

resource "google_storage_bucket_object" "region_folders" {
  for_each = toset(flatten([
    for region in var.forecast_regions : [
      "${region}/config/.keep",
      "${region}/support/.keep",
      "${region}/output/.keep",
      "${region}/raw/.keep",
      "${region}/intermediate/.keep",
    ]
  ]))

  bucket  = google_storage_bucket.main.name
  name    = each.key
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

  # Cleanup policy for dev
  dynamic "cleanup_policies" {
    for_each = var.environment == "dev" ? [1] : []
    content {
      id     = "delete-old-images"
      action = "DELETE"
      condition {
        older_than = "2592000s" # 30 days
      }
    }
  }
}

# -----------------------------------------------------------------------------
# Model Weights Bucket (separate for versioning/security)
# -----------------------------------------------------------------------------

resource "google_storage_bucket" "weights" {
  name     = "${var.name_prefix}-${var.environment}-weights-${var.project_id}"
  project  = var.project_id
  location = var.region

  force_destroy = false # Never auto-delete model weights

  uniform_bucket_level_access = true

  versioning {
    enabled = true # Always version model weights
  }

  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "model-weights"
  }
}

# -----------------------------------------------------------------------------
# IAM Bindings
# -----------------------------------------------------------------------------

# Service account for pipeline operations
resource "google_service_account" "pipeline" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-${var.environment}-pipeline"
  display_name = "Monsoon Pipeline Service Account (${var.environment})"
}

# Pipeline SA can read/write main bucket
resource "google_storage_bucket_iam_member" "pipeline_main_bucket" {
  bucket = google_storage_bucket.main.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

# Pipeline SA can read weights bucket
resource "google_storage_bucket_iam_member" "pipeline_weights_bucket" {
  bucket = google_storage_bucket.weights.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

# Pipeline SA can pull container images
resource "google_artifact_registry_repository_iam_member" "pipeline_ar" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.containers.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.pipeline.email}"
}

# Batch VMs run as the pipeline SA and must report task state back to Batch.
resource "google_project_iam_member" "pipeline_batch_agent_reporter" {
  project = var.project_id
  role    = "roles/batch.agentReporter"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# Batch task stdout/stderr is written to Cloud Logging by the job service account.
resource "google_project_iam_member" "pipeline_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}
