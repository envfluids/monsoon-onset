# -----------------------------------------------------------------------------
# Orchestration Module
# Cloud Scheduler, Cloud Workflows, Pub/Sub
# -----------------------------------------------------------------------------

locals {
  models_in_use = distinct(flatten([for r, cfg in var.regions : cfg.models]))
  gpu_models_in_use = [
    for model in local.models_in_use : model
    if model != "gencast"
  ]

  regions_by_model = {
    for m in distinct(flatten([for r, cfg in var.regions : cfg.models])) :
    m => [for r, cfg in var.regions : r if contains(cfg.models, m)]
  }

  # Map model name → batch container image
  model_images = {
    aifs      = var.batch_job_template.aifs_image
    aifs_ens  = var.batch_job_template.aifs_image
    neuralgcm = var.batch_job_template.neuralgcm_image
    gencast   = var.batch_job_template.gencast_image
  }

  gencast_tpu_startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail

    LOG_FILE=/var/log/monsoon-gencast-startup.log
    exec > >(tee -a "$LOG_FILE" | logger -t monsoon-gencast-startup -s 2>/dev/console) 2>&1

    metadata() {
      curl -fsH "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
    }

    DATE=$(metadata date)
    FORECAST_REGIONS=$(metadata forecast-regions)
    COMMON_BUCKET=$(metadata common-bucket)
    REGION_BUCKETS=$(metadata region-buckets)
    GENCAST_IMAGE=$(metadata gencast-image)
    ARTIFACT_REGISTRY_HOST=$(metadata artifact-registry-host)
    EXPECTED_GLOBAL_DEVICES=$(metadata expected-global-devices)
    EXPECTED_LOCAL_DEVICES=$(metadata expected-local-devices)
    EXPECTED_PROCESS_COUNT=$(metadata expected-process-count)
    ENSEMBLE_MEMBERS=$(metadata ensemble-members)

    on_error() {
      status=$?
      host=$(hostname)
      marker="intermediate/gencast_$${DATE}_failed_$${host}"
      printf "GenCast TPU startup failed on %s for %s with exit code %s\n" "$host" "$DATE" "$status" \
        | gcloud storage cp - "gs://$${COMMON_BUCKET}/$${marker}" || true
      exit "$status"
    }
    trap on_error ERR

    echo "Starting GenCast TPU run date=$DATE image=$GENCAST_IMAGE expected_global_devices=$EXPECTED_GLOBAL_DEVICES expected_local_devices=$EXPECTED_LOCAL_DEVICES expected_process_count=$EXPECTED_PROCESS_COUNT"
    systemctl start docker
    gcloud auth configure-docker "$ARTIFACT_REGISTRY_HOST" --quiet
    docker pull "$GENCAST_IMAGE"
    docker run --rm --privileged --net=host --name "monsoon-gencast-$DATE" \
      -e DATE="$DATE" \
      -e FORECAST_REGIONS="$FORECAST_REGIONS" \
      -e GCS_COMMON_BUCKET="$COMMON_BUCKET" \
      -e GCS_REGION_BUCKETS="$REGION_BUCKETS" \
      -e UPLOAD_FULL_FIELD="true" \
      -e GENCAST_JAX_DISTRIBUTED="true" \
      -e GENCAST_EXPECTED_GLOBAL_DEVICES="$EXPECTED_GLOBAL_DEVICES" \
      -e GENCAST_EXPECTED_LOCAL_DEVICES="$EXPECTED_LOCAL_DEVICES" \
      -e GENCAST_EXPECTED_PROCESS_COUNT="$EXPECTED_PROCESS_COUNT" \
      -e GENCAST_ENSEMBLE_MEMBERS="$ENSEMBLE_MEMBERS" \
      "$GENCAST_IMAGE"
    echo "Finished GenCast TPU run date=$DATE"
  EOT
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

resource "google_project_iam_member" "workflow_tpu_admin" {
  project = var.project_id
  role    = "roles/tpu.admin"
  member  = "serviceAccount:${google_service_account.workflow.email}"
}

resource "google_project_iam_member" "workflow_invoker" {
  project = var.project_id
  role    = "roles/workflows.invoker"
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

resource "google_pubsub_topic" "pipeline_completions" {
  name    = "${var.name_prefix}-${var.environment}-pipeline-completions"
  project = var.project_id

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

resource "google_pubsub_topic" "dead_letter" {
  name    = "${var.name_prefix}-${var.environment}-dead-letter"
  project = var.project_id

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
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

    batch_config               = var.batch_job_template
    common_bucket              = var.common_bucket
    region_buckets             = var.region_buckets
    regions                    = var.regions
    models_in_use              = local.models_in_use
    gpu_models_in_use          = local.gpu_models_in_use
    regions_by_model           = local.regions_by_model
    model_images               = local.model_images
    tpu_config                 = var.gencast_tpu_template
    gencast_tpu_startup_script = local.gencast_tpu_startup_script
    pipeline_sa                = var.pipeline_service_account_email
    full_field_models          = var.full_field_models
  })

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
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
