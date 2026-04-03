# -----------------------------------------------------------------------------
# Orchestration Module Outputs
# -----------------------------------------------------------------------------

output "workflow_id" {
  description = "Cloud Workflow ID"
  value       = google_workflows_workflow.main_pipeline.id
}

output "workflow_name" {
  description = "Cloud Workflow name"
  value       = google_workflows_workflow.main_pipeline.name
}

output "workflow_url" {
  description = "Cloud Workflow execution URL"
  value       = "https://console.cloud.google.com/workflows/workflow/${var.region}/${google_workflows_workflow.main_pipeline.name}?project=${var.project_id}"
}

output "scheduler_jobs" {
  description = "Cloud Scheduler job names"
  value       = { for k, v in google_cloud_scheduler_job.pipeline_trigger : k => v.name }
}

output "workflow_service_account" {
  description = "Workflow service account email"
  value       = google_service_account.workflow.email
}

output "pubsub_topics" {
  description = "Pub/Sub topic names"
  value = {
    triggers    = google_pubsub_topic.pipeline_triggers.name
    completions = google_pubsub_topic.pipeline_completions.name
    dead_letter = google_pubsub_topic.dead_letter.name
  }
}
