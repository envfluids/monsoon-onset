# -----------------------------------------------------------------------------
# Storage Module Outputs
# -----------------------------------------------------------------------------

output "common_bucket_name" {
  description = "Common bucket name (ICs, weights, full-field, intermediate)"
  value       = google_storage_bucket.common.name
}

output "common_bucket_url" {
  description = "Common bucket URL"
  value       = google_storage_bucket.common.url
}

output "region_bucket_names" {
  description = "Map of forecast region to region-specific data bucket name"
  value       = { for r, b in google_storage_bucket.region : r => b.name }
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
