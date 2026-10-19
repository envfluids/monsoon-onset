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

# Alert: delivery deadline missed, or a Batch job hit its retry cap. Both are
# logged by the workflow at WARNING (below the >= ERROR that pipeline_failure uses).
resource "google_monitoring_alert_policy" "delivery_late" {
  count = var.enable_alerts && var.enable_delivery_alert ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Delivery Late"
  combiner     = "OR"

  conditions {
    display_name = "Delivery marker missing or Batch retries exhausted"

    condition_matched_log {
      filter = <<-EOT
        resource.type="workflows.googleapis.com/Workflow"
        (textPayload:"DELIVERY_LATE" OR textPayload:"BATCH_RETRY_CAP")
      EOT
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "3600s" # Max 1 notification per hour
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "DELIVERY_LATE: today's India AIFS ensemble done marker was missing at the check time (often a GPU stockout; see the AIFS_ENS_v2 Batch job's status events). BATCH_RETRY_CAP: a Batch job failed its maximum attempts and will not be resubmitted for that date. Check Cloud Logging for the workflow message."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "warning"
  }
}

# Alert: the ensemble fallback ladder was used (a fallback job was submitted, or
# one started first and owns the date). Logged by the workflow at WARNING.
# A policy with a log-match condition takes exactly one condition, so the claim
# token (a different resource type) has its own policy below.
resource "google_monitoring_alert_policy" "ensemble_fallback" {
  count = var.enable_alerts && var.enable_ensemble_fallback_alert ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Ensemble Fallback Used"
  combiner     = "OR"

  conditions {
    display_name = "Fallback Batch job submitted or won"

    condition_matched_log {
      filter = <<-EOT
        resource.type="workflows.googleapis.com/Workflow"
        (textPayload:"ENSEMBLE_FALLBACK_SUBMITTED" OR textPayload:"ENSEMBLE_FALLBACK_WON")
      EOT
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "300s" # the submit and the win are minutes apart; both get through
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "ENSEMBLE_FALLBACK_SUBMITTED: the India AIFS ensemble's Batch job hadn't started after the ladder's wait (GPU stockout), so a fallback job was queued beside it. ENSEMBLE_FALLBACK_WON: a fallback job started first and owns the date; the others were cancelled. A 1-GPU win (fb2) delivers about 25 min after 14:00 IST. See the workflow log line and the jobs' status events; the claim is gs://<common>/batch-state/AIFS_ENS_v2/<date>/winner."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "warning"
  }
}

# Alert: a ladder job couldn't write its claim. Logged by the Batch job's
# container; Batch logs all container stderr at ERROR, so this matches the
# token, not the severity.
resource "google_monitoring_alert_policy" "ensemble_ladder_claim_unavailable" {
  count = var.enable_alerts && var.enable_ensemble_fallback_alert ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Ensemble Ladder Claim Unavailable"
  combiner     = "OR"

  conditions {
    display_name = "Ladder claim unavailable"

    condition_matched_log {
      filter = <<-EOT
        resource.type="batch.googleapis.com/Job"
        textPayload:"ENSEMBLE_LADDER_CLAIM_UNAVAILABLE"
      EOT
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "ENSEMBLE_LADDER_CLAIM_UNAVAILABLE (Batch job log): GCS refused or timed out the ensemble ladder's claim 3 times; the primary ran without a claim and fallback jobs exited without running. Check GCS status and the job's earlier 'Ladder claim attempt' warnings. If a fallback was still queued and GCS recovers before it starts, it can claim the date and the primary gets cancelled; cancel that fallback by hand."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "high"
  }
}

# Alert: DryWetCast ran without its subdistrict products. These problems are
# deliberately non-fatal (the execution succeeds), so the Cloud Run Job Failure
# alert never sees them; the wrapper logs one of these tokens at ERROR instead.
resource "google_monitoring_alert_policy" "drywetcast_subdistrict" {
  count = var.enable_alerts ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] DryWetCast Subdistrict Products Missing"
  combiner     = "OR"

  conditions {
    display_name = "Subdistrict bundle unavailable or subdistrict step failed"

    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="${var.name_prefix}-${var.environment}-drywetcast"
        (textPayload:"DRYWETCAST_SUBDISTRICT_UNAVAILABLE" OR textPayload:"DRYWETCAST_SUBDISTRICT_FAILED")
      EOT
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "3600s" # Max 1 notification per hour
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "DRYWETCAST_SUBDISTRICT_UNAVAILABLE: the subdistrict bundle could not be copied or failed its manifest check, so every config ran without subdistrict products. DRYWETCAST_SUBDISTRICT_FAILED: DryWetCast failed with the bundle for one config, which was rerun without it. The core products and outlook were still delivered; see the day's manifest.json (subdistrict_bundle, this_run.<config>.subdistrict)."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "warning"
  }
}

# Alert: a sync item failed in SYNC_ITEM_MAX_ATTEMPTS separate executions and
# will not be retried. Each failed execution also fires the Cloud Run Job
# Failure alert; this one says the job has given up.
resource "google_monitoring_alert_policy" "sync_gave_up" {
  count = var.enable_alerts ? 1 : 0

  project      = var.project_id
  display_name = "[${upper(var.environment)}] Monsoon Sync Failing Repeatedly"
  combiner     = "OR"

  conditions {
    display_name = "Sync items reached their attempt cap"

    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_run_job"
        resource.labels.job_name="${var.name_prefix}-${var.environment}-sync"
        textPayload:"SYNC_ITEMS_GAVE_UP"
      EOT
    }
  }

  notification_channels = [for ch in google_monitoring_notification_channel.email : ch.id]

  alert_strategy {
    notification_rate_limit {
      period = "3600s" # Max 1 notification per hour
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "SYNC_ITEMS_GAVE_UP: the listed sync items (e.g. DryWetCast configs bound for Google Drive) failed in their maximum number of executions and won't be retried. The bucket outputs are unaffected. Run the sync job with ACTION=probe to check the Drive login and folder, fix it, then delete sync-items/<date>/<type>/<name>.failed.json in the region bucket to let the next pass retry."
    mime_type = "text/markdown"
  }

  user_labels = {
    environment = var.environment
    severity    = "warning"
  }
}

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

  # The Monitoring API normalizes dashboard_json on read (injects etag/name,
  # default plotType/targetAxis, columns as a string), producing a perpetual
  # in-place diff. Ignore it so applies stay clean; update the dashboard by
  # editing dashboard_json above and temporarily removing this ignore.
  lifecycle {
    ignore_changes = [dashboard_json]
  }
}
