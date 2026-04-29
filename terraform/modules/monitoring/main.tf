# -----------------------------------------------------------------------------
# Monitoring Module
# Cloud Logging, Cloud Monitoring dashboards, Alert policies
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Log Sink - Export logs to BigQuery for analysis (optional)
# -----------------------------------------------------------------------------

resource "google_logging_project_sink" "pipeline_logs" {
  count = var.export_logs_to_bigquery ? 1 : 0

  name        = "${var.name_prefix}-${var.environment}-pipeline-logs"
  project     = var.project_id
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.logs[0].dataset_id}"

  filter = <<-EOT
    resource.type="cloud_run_job" OR
    resource.type="cloud_batch_job" OR
    resource.type="workflows.googleapis.com/Workflow"
    labels.environment="${var.environment}"
  EOT

  unique_writer_identity = true
}

resource "google_bigquery_dataset" "logs" {
  count = var.export_logs_to_bigquery ? 1 : 0

  dataset_id = "${var.name_prefix}_${var.environment}_logs"
  project    = var.project_id
  location   = var.region

  default_table_expiration_ms = 7776000000 # 90 days

  labels = {
    environment = var.environment
    managed_by  = "terraform"
  }
}

# Grant the log sink writer access to BigQuery
resource "google_bigquery_dataset_iam_member" "logs_writer" {
  count = var.export_logs_to_bigquery ? 1 : 0

  project    = var.project_id
  dataset_id = google_bigquery_dataset.logs[0].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.pipeline_logs[0].writer_identity
}

# -----------------------------------------------------------------------------
# Notification Channels
# -----------------------------------------------------------------------------

resource "google_monitoring_notification_channel" "email" {
  for_each = var.enable_alerts ? toset(var.notification_emails) : []

  project      = var.project_id
  display_name = "Email: ${each.key}"
  type         = "email"

  labels = {
    email_address = each.key
  }
}

# -----------------------------------------------------------------------------
# Alert Policies
# -----------------------------------------------------------------------------

# Alert: Pipeline execution failed
resource "google_monitoring_alert_policy" "pipeline_failure" {
  count = var.enable_alerts ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Pipeline Failure"
  combiner     = "OR"

  conditions {
    display_name = "Workflow execution failed"

    condition_matched_log {
      filter = <<-EOT
        resource.type="workflows.googleapis.com/Workflow"
        severity>=ERROR
        labels.environment="${var.environment}"
      EOT
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "300s" # Max 1 notification per 5 minutes
    }
  }

  documentation {
    content   = "The monsoon forecast pipeline has failed. Check Cloud Logging for details."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "critical"
  }
}

# Alert: Cloud Run job failure
resource "google_monitoring_alert_policy" "cloudrun_failure" {
  count = var.enable_alerts ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Cloud Run Job Failure"
  combiner     = "OR"

  conditions {
    display_name = "Cloud Run job failed"

    condition_threshold {
      filter          = "resource.type=\"cloud_run_job\" AND metric.type=\"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result=\"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  documentation {
    content   = "A Cloud Run job in the monsoon pipeline has failed."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "high"
  }
}

# Alert: No pipeline runs in expected window
resource "google_monitoring_alert_policy" "pipeline_stale" {
  count = var.enable_alerts && var.environment == "prod" ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Pipeline Stale"
  combiner     = "OR"

  conditions {
    display_name = "No successful pipeline run in 6 hours"

    condition_absent {
      filter   = "resource.type=\"workflows.googleapis.com/Workflow\" AND metric.type=\"logging.googleapis.com/log_entry_count\" AND labels.severity=\"INFO\""
      duration = "21600s" # 6 hours

      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  documentation {
    content   = "No successful monsoon pipeline execution in the past 6 hours. This may indicate upstream data availability issues."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "medium"
  }
}

# -----------------------------------------------------------------------------
# Monitoring Dashboard
# -----------------------------------------------------------------------------

resource "google_monitoring_dashboard" "pipeline" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "Monsoon Pipeline (${var.environment})"
    gridLayout = {
      columns = 2
      widgets = [
        {
          title = "Workflow Executions"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "resource.type=\"workflows.googleapis.com/Workflow\" AND metric.type=\"workflows.googleapis.com/finished_execution_count\""
                  aggregation = {
                    alignmentPeriod  = "3600s"
                    perSeriesAligner = "ALIGN_SUM"
                  }
                }
              }
            }]
          }
        },
        {
          title = "Cloud Run Job Executions"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "resource.type=\"cloud_run_job\" AND metric.type=\"run.googleapis.com/job/completed_execution_count\""
                  aggregation = {
                    alignmentPeriod  = "3600s"
                    perSeriesAligner = "ALIGN_SUM"
                  }
                }
              }
            }]
          }
        },
        {
          title = "Cloud Batch Jobs"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "resource.type=\"batch.googleapis.com/Job\" AND metric.type=\"batch.googleapis.com/job/state\""
                  aggregation = {
                    alignmentPeriod  = "3600s"
                    perSeriesAligner = "ALIGN_COUNT"
                  }
                }
              }
            }]
          }
        },
        {
          title = "GCS Operations"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "resource.type=\"gcs_bucket\" AND metric.type=\"storage.googleapis.com/api/request_count\""
                  aggregation = {
                    alignmentPeriod  = "3600s"
                    perSeriesAligner = "ALIGN_SUM"
                  }
                }
              }
            }]
          }
        },
        {
          title = "Error Logs"
          logsPanel = {
            filter = "severity>=ERROR labels.environment=\"${var.environment}\""
          }
        },
        {
          title = "Pipeline Status"
          text = {
            content = "Check workflow executions and job status above. Green = healthy, errors shown in log panel."
            format  = "MARKDOWN"
          }
        }
      ]
    }
  })
}
