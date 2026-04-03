# -----------------------------------------------------------------------------
# Storage Module Outputs
# -----------------------------------------------------------------------------

output "bucket_name" {
  description = "Main data bucket name"
  value       = google_storage_bucket.main.name
}

output "bucket_url" {
  description = "Main data bucket URL"
  value       = google_storage_bucket.main.url
}

output "weights_bucket_name" {
  description = "Model weights bucket name"
  value       = google_storage_bucket.weights.name
}

output "weights_bucket_url" {
  description = "Model weights bucket URL"
  value       = google_storage_bucket.weights.url
}

output "artifact_registry_url" {
  description = "Artifact Registry URL for container images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.containers.name}"
}

output "artifact_registry_name" {
  description = "Artifact Registry repository name"
  value       = google_artifact_registry_repository.containers.name
}

output "pipeline_service_account_email" {
  description = "Pipeline service account email"
  value       = google_service_account.pipeline.email
}

output "pipeline_service_account_name" {
  description = "Pipeline service account name"
  value       = google_service_account.pipeline.name
}
