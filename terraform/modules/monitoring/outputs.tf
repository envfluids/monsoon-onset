# -----------------------------------------------------------------------------
# Monitoring Module Outputs
# -----------------------------------------------------------------------------

output "dashboard_url" {
  description = "Cloud Monitoring dashboard URL"
  value       = "https://console.cloud.google.com/monitoring/dashboards/builder/${google_monitoring_dashboard.pipeline.id}?project=${var.project_id}"
}

output "dashboard_id" {
  description = "Cloud Monitoring dashboard ID"
  value       = google_monitoring_dashboard.pipeline.id
}

output "alert_policy_ids" {
  description = "Alert policy IDs"
  value = compact([
    var.enable_alerts ? google_monitoring_alert_policy.pipeline_failure[0].id : null,
    var.enable_alerts ? google_monitoring_alert_policy.cloudrun_failure[0].id : null,
    var.enable_alerts && var.environment == "prod" ? google_monitoring_alert_policy.pipeline_stale[0].id : null,
  ])
}

output "notification_channel_ids" {
  description = "Notification channel IDs"
  value       = [for ch in google_monitoring_notification_channel.email : ch.id]
}

output "bigquery_dataset" {
  description = "BigQuery dataset for logs (if enabled)"
  value       = var.export_logs_to_bigquery ? google_bigquery_dataset.logs[0].dataset_id : null
}
