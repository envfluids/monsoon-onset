# -----------------------------------------------------------------------------
# Orchestration Module
# Cloud Scheduler, Cloud Workflows, Pub/Sub
# -----------------------------------------------------------------------------

locals {
  models_in_use = distinct(flatten([for r, cfg in var.regions : cfg.models]))
  batch_models_in_use = [
    for model in local.models_in_use : model
    if model != "gencast"
  ]

  # Map model name → batch container image
  model_images = {
    AIFS_single_v2 = var.batch_job_template.aifs_v2_image
    AIFS_ENS_v2    = var.batch_job_template.aifs_ens_v2_image
    neuralgcm      = var.batch_job_template.neuralgcm_image
    blend          = var.batch_job_template.blend_image
    gencast        = var.batch_job_template.gencast_image
  }
}

data "google_storage_project_service_account" "gcs_account" {
  project = var.project_id
}

resource "google_project_service" "eventarc" {
  project            = var.project_id
  service            = "eventarc.googleapis.com"
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Service Account for Workflows
# -----------------------------------------------------------------------------

resource "google_service_account" "workflow" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-${var.environment}-workflow"
  display_name = "Monsoon Workflow Service Account (${var.environment})"
}

resource "google_project_iam_member" "workflow_run_invoker" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_cloud_run_v2_service_iam_member" "workflow_pipeline_state_invoker" {
  project  = var.project_id
  location = var.region
  name     = var.pipeline_state_service_name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "workflow_batch_jobs_editor" {
  project = var.project_id
  role    = "roles/batch.jobsEditor"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "workflow_invoker" {
  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "workflow_eventarc_receiver" {
  project = var.project_id
  role    = "roles/eventarc.eventReceiver"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_storage_bucket_iam_member" "workflow_common_gcs_object_admin" {
  bucket = var.common_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_storage_bucket_iam_member" "workflow_region_gcs_object_admin" {
  for_each = var.region_buckets

  bucket = each.value
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "workflow_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_service_account_iam_member" "workflow_impersonate_pipeline" {
  service_account_id = var.pipeline_service_account_id
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

resource "google_pubsub_topic_iam_member" "gcs_pipeline_trigger_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.pipeline_triggers.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${data.google_storage_project_service_account.gcs_account.email_address}"
}

resource "google_storage_notification" "common_intermediate_finalized" {
  bucket             = var.common_bucket
  topic              = google_pubsub_topic.pipeline_triggers.id
  payload_format     = "JSON_API_V1"
  event_types        = ["OBJECT_FINALIZE"]
  object_name_prefix = "intermediate/"

  depends_on = [google_pubsub_topic_iam_member.gcs_pipeline_trigger_publisher]
}

# -----------------------------------------------------------------------------
# Cloud Workflows — Main Pipeline (region-agnostic)
# -----------------------------------------------------------------------------

resource "google_workflows_workflow" "main_pipeline" {
  name                    = "${var.name_prefix}-${var.environment}-pipeline"
  project                 = var.project_id
  region                  = var.region
  service_account         = google_service_account.workflow.email
  call_log_level          = var.call_log_level
  execution_history_level = var.execution_history_level
  deletion_protection     = false

  source_contents = templatefile("${path.module}/workflow.yaml.tpl", {
    project_id         = var.project_id
    region             = var.region
    environment        = var.environment
    cloud_run_jobs     = var.cloud_run_services
    pipeline_state_url = var.pipeline_state_url

    batch_config        = var.batch_job_template
    common_bucket       = var.common_bucket
    region_buckets      = var.region_buckets
    regions             = var.regions
    models_in_use       = local.models_in_use
    batch_models_in_use = local.batch_models_in_use
    model_images        = local.model_images
    tpu_config          = var.gencast_tpu_dispatch_template
    pipeline_sa         = var.pipeline_service_account_email
    full_field_models   = var.full_field_models
  })

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "google_eventarc_trigger" "pipeline_intermediate_events" {
  name     = "${var.name_prefix}-${var.environment}-pipeline-intermediate-events"
  project  = var.project_id
  location = var.region

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }

  transport {
    pubsub {
      topic = google_pubsub_topic.pipeline_triggers.id
    }
  }

  destination {
    workflow = google_workflows_workflow.main_pipeline.id
  }

  service_account = google_service_account.workflow.email

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }

  depends_on = [
    google_project_service.eventarc,
    google_project_iam_member.workflow_eventarc_receiver,
    google_project_iam_member.workflow_invoker,
  ]
}

# -----------------------------------------------------------------------------
# Cloud Scheduler — single trigger, no per-region argument
# -----------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "pipeline_trigger" {
  name        = "${var.name_prefix}-${var.environment}-pipeline-trigger"
  project     = var.project_id
  region      = var.region
  description = "Triggers monsoon multi-region pipeline"
  schedule    = var.pipeline_schedule
  time_zone   = "UTC"

  http_target {
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.main_pipeline.id}/executions"
    http_method = "POST"

    body = base64encode(jsonencode({
      argument = jsonencode({})
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
