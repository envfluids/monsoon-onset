# -----------------------------------------------------------------------------
# Orchestration Module
# Cloud Scheduler, Cloud Workflows, Pub/Sub
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Service Account for Workflows
# -----------------------------------------------------------------------------

resource "google_service_account" "workflow" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-${var.environment}-workflow"
  display_name = "Monsoon Workflow Service Account (${var.environment})"
}

# Workflow SA can invoke and run Cloud Run jobs (run.invoker + run.jobs.runWithOverrides)
resource "google_project_iam_member" "workflow_run_invoker" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

# Workflow SA can use TPU
resource "google_project_iam_member" "workflow_tpu_admin" {
  project = var.project_id
  role    = "roles/tpu.admin"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

# Workflow SA can create Workflow executions (required for Cloud Scheduler -> Workflows)
resource "google_project_iam_member" "workflow_invoker" {
  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

# Workflow SA can read GCS intermediate files (latest_date.txt, latest.txt)
resource "google_storage_bucket_iam_member" "workflow_gcs_reader" {
  bucket = "monsoon-${var.environment}-data-${var.project_id}"
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.workflow.email}"
}

# Workflow SA can write logs
resource "google_project_iam_member" "workflow_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

# Workflow SA can impersonate the pipeline SA (to submit Cloud Run jobs)
resource "google_service_account_iam_member" "workflow_impersonate_pipeline" {
  service_account_id = var.pipeline_service_account_id
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workflow.email}"
}

# Workflow SA can impersonate the TPU SA
resource "google_service_account_iam_member" "workflow_impersonate_tpu" {
  service_account_id = var.tpu_service_account_id
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workflow.email}"
}

# -----------------------------------------------------------------------------
# Pub/Sub Topics for Pipeline Events
# -----------------------------------------------------------------------------

resource "google_pubsub_topic" "pipeline_triggers" {
  name    = "${var.name_prefix}-${var.environment}-pipeline-triggers"
  project = var.project_id

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "google_pubsub_topic" "pipeline_completions" {
  name    = "${var.name_prefix}-${var.environment}-pipeline-completions"
  project = var.project_id

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

# Dead letter topic for failed messages
resource "google_pubsub_topic" "dead_letter" {
  name    = "${var.name_prefix}-${var.environment}-dead-letter"
  project = var.project_id

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Cloud Workflows - Main Pipeline
# -----------------------------------------------------------------------------

resource "google_workflows_workflow" "main_pipeline" {
  name                = "${var.name_prefix}-${var.environment}-pipeline"
  project             = var.project_id
  region              = var.region
  service_account     = google_service_account.workflow.email
  call_log_level      = var.call_log_level
  deletion_protection = false

  source_contents = templatefile("${path.module}/workflow.yaml.tpl", {
    project_id      = var.project_id
    region          = var.region
    environment     = var.environment
    cloud_run_jobs  = var.cloud_run_services
    batch_config    = var.batch_job_template
    weights_bucket  = var.weights_bucket
  })

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Cloud Scheduler - Trigger Pipeline
# -----------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "pipeline_trigger" {
  for_each = toset(var.forecast_regions)

  name        = "${var.name_prefix}-${var.environment}-trigger-${each.key}"
  project     = var.project_id
  region      = var.region
  description = "Triggers monsoon pipeline for ${each.key} region"
  schedule    = var.pipeline_schedule
  time_zone   = "UTC"

  http_target {
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.main_pipeline.id}/executions"
    http_method = "POST"

    body = base64encode(jsonencode({
      argument = jsonencode({
        region = each.key
        # date intentionally omitted: workflow determines the latest available date
      })
    }))

    oauth_token {
      service_account_email = google_service_account.workflow.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  retry_config {
    retry_count          = 3
    min_backoff_duration = "5s"
    max_backoff_duration = "300s"
  }
}

