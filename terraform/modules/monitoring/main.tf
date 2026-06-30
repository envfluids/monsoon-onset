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
# Log-based metric: per-stage success/failure
# Each pipeline container emits one JSON line
# {"event":"stage_result", "stage":..., "region":..., "status":"success|failure"}
# on success and failure. This metric turns those into a counter keyed by
# stage/region/status. dev and prod are separate projects, so it is inherently
# per-environment.
# -----------------------------------------------------------------------------

locals {
  stage_metric_type = "logging.googleapis.com/user/${var.name_prefix}_${var.environment}_stage_result_count"
}

resource "google_logging_metric" "stage_result" {
  project = var.project_id
  name    = "${var.name_prefix}_${var.environment}_stage_result_count"
  filter  = "jsonPayload.event=\"stage_result\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    labels {
      key         = "stage"
      value_type  = "STRING"
      description = "Pipeline stage (downloader, aifs, aifs_ens, neuralgcm, gencast, gencast_dispatch, blend, sync)"
    }
    labels {
      key         = "region"
      value_type  = "STRING"
      description = "Forecast region, or empty for region-agnostic stages"
    }
    labels {
      key         = "status"
      value_type  = "STRING"
      description = "success or failure"
    }
  }

  label_extractors = {
    "stage"  = "EXTRACT(jsonPayload.stage)"
    "region" = "EXTRACT(jsonPayload.region)"
    "status" = "EXTRACT(jsonPayload.status)"
  }
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

# Alert: a pipeline stage emitted a failure (from the log-based metric)
resource "google_monitoring_alert_policy" "stage_failure" {
  count = var.enable_alerts ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Stage Failure"
  combiner     = "OR"

  conditions {
    display_name = "A pipeline stage emitted a failure"

    condition_threshold {
      filter          = "metric.type=\"${local.stage_metric_type}\" AND metric.label.status=\"failure\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.stage", "metric.label.region"]
      }
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
  }

  documentation {
    content   = "A monsoon pipeline stage emitted a structured failure (jsonPayload.event=stage_result, status=failure). The incident labels identify the stage and region; see the dashboard 'Stage Failures' log panel for the error text."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "high"
  }
}

# -----------------------------------------------------------------------------
# Monitoring Dashboard
# -----------------------------------------------------------------------------

resource "google_monitoring_dashboard" "pipeline" {
  project = var.project_id

  # dashboard_json is intentionally NOT ignored: edit it here and let CI apply.
  # plotType/targetAxis are set explicitly and we use mosaicLayout to avoid the
  # API normalization that previously caused a perpetual in-place diff. Do not
  # re-add a lifecycle ignore_changes block, or future edits would silently not
  # deploy.
  dashboard_json = jsonencode({
    displayName = "Monsoon Pipeline (${var.environment})"
    mosaicLayout = {
      columns = 12
      tiles = concat([
        {
          xPos = 0, yPos = 0, width = 3, height = 4
          widget = {
            title = "Workflow Succeeded (24h)"
            scorecard = {
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"workflows.googleapis.com/finished_execution_count\" AND resource.type=\"workflows.googleapis.com/Workflow\" AND metric.label.status=\"SUCCEEDED\""
                  aggregation = {
                    alignmentPeriod    = "86400s"
                    perSeriesAligner   = "ALIGN_SUM"
                    crossSeriesReducer = "REDUCE_SUM"
                  }
                }
              }
              sparkChartView = { sparkChartType = "SPARK_LINE" }
              thresholds     = [{ value = 1, color = "GREEN", direction = "ABOVE" }]
            }
          }
        },
        {
          xPos = 3, yPos = 0, width = 3, height = 4
          widget = {
            title = "Workflow Failed (24h)"
            scorecard = {
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"workflows.googleapis.com/finished_execution_count\" AND resource.type=\"workflows.googleapis.com/Workflow\" AND metric.label.status=\"FAILED\""
                  aggregation = {
                    alignmentPeriod    = "86400s"
                    perSeriesAligner   = "ALIGN_SUM"
                    crossSeriesReducer = "REDUCE_SUM"
                  }
                }
              }
              sparkChartView = { sparkChartType = "SPARK_BAR" }
              thresholds     = [{ value = 0, color = "RED", direction = "ABOVE" }]
            }
          }
        },
        {
          xPos = 6, yPos = 0, width = 3, height = 4
          widget = {
            title = "Stage Failures (24h)"
            scorecard = {
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"${local.stage_metric_type}\" AND metric.label.status=\"failure\""
                  aggregation = {
                    alignmentPeriod    = "86400s"
                    perSeriesAligner   = "ALIGN_SUM"
                    crossSeriesReducer = "REDUCE_SUM"
                  }
                }
              }
              sparkChartView = { sparkChartType = "SPARK_BAR" }
              thresholds     = [{ value = 0, color = "RED", direction = "ABOVE" }]
            }
          }
        },
        {
          xPos = 9, yPos = 0, width = 3, height = 4
          widget = {
            title = "Workflow Success (6h)"
            scorecard = {
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"workflows.googleapis.com/finished_execution_count\" AND resource.type=\"workflows.googleapis.com/Workflow\" AND metric.label.status=\"SUCCEEDED\""
                  aggregation = {
                    alignmentPeriod    = "21600s"
                    perSeriesAligner   = "ALIGN_SUM"
                    crossSeriesReducer = "REDUCE_SUM"
                  }
                }
              }
              sparkChartView = { sparkChartType = "SPARK_LINE" }
              thresholds     = [{ value = 1, color = "GREEN", direction = "ABOVE" }]
            }
          }
        },
        {
          xPos = 0, yPos = 4, width = 8, height = 5
          widget = {
            title = "Stage Results (success vs failure)"
            xyChart = {
              dataSets = [
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"${local.stage_metric_type}\" AND metric.label.status=\"success\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_SUM"
                        crossSeriesReducer = "REDUCE_SUM"
                        groupByFields      = ["metric.label.stage"]
                      }
                    }
                  }
                  plotType       = "STACKED_BAR"
                  targetAxis     = "Y1"
                  legendTemplate = "$${metric.label.stage} ok"
                },
                {
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"${local.stage_metric_type}\" AND metric.label.status=\"failure\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_SUM"
                        crossSeriesReducer = "REDUCE_SUM"
                        groupByFields      = ["metric.label.stage"]
                      }
                    }
                  }
                  plotType       = "STACKED_BAR"
                  targetAxis     = "Y1"
                  legendTemplate = "$${metric.label.stage} FAIL"
                }
              ]
            }
          }
        },
        {
          xPos = 8, yPos = 4, width = 4, height = 5
          widget = {
            title = "Stage Failures (error detail)"
            logsPanel = {
              filter        = "jsonPayload.event=\"stage_result\" AND jsonPayload.status=\"failure\""
              resourceNames = ["projects/${var.project_id}"]
            }
          }
        },
        {
          xPos = 0, yPos = 9, width = 6, height = 4
          widget = {
            title = "Cloud Run Jobs (success/failure)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/job/completed_execution_count\" AND resource.type=\"cloud_run_job\""
                    aggregation = {
                      alignmentPeriod    = "3600s"
                      perSeriesAligner   = "ALIGN_SUM"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["resource.label.job_name", "metric.label.result"]
                    }
                  }
                }
                plotType       = "STACKED_BAR"
                targetAxis     = "Y1"
                legendTemplate = "$${resource.label.job_name} $${metric.label.result}"
              }]
            }
          }
        }
        ], var.enable_alerts ? [
        {
          xPos = 6, yPos = 9, width = 6, height = 4
          widget = {
            title      = "Stage Failure Alert"
            alertChart = { name = google_monitoring_alert_policy.stage_failure[0].id }
          }
        }
      ] : [])
    }
  })
}
