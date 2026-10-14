# -----------------------------------------------------------------------------
# Monitoring Module Variables
# -----------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name (dev, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
  default     = "monsoon"
}

variable "enable_alerts" {
  description = "Enable alert policies"
  type        = bool
  default     = false
}

variable "enable_ensemble_fallback_alert" {
  description = "Email when the ensemble fallback ladder submits a fallback job (ENSEMBLE_FALLBACK_SUBMITTED) or a fallback job wins (ENSEMBLE_FALLBACK_WON). Needs enable_alerts."
  type        = bool
  default     = false
}

variable "enable_delivery_alert" {
  description = "Alert on DELIVERY_LATE (delivery deadline missed) and BATCH_RETRY_CAP (a Batch job stopped being retried) workflow logs. Needs enable_alerts."
  type        = bool
  default     = false
}

variable "notification_emails" {
  description = "Email addresses for alert notifications"
  type        = list(string)
  default     = []
}

variable "export_logs_to_bigquery" {
  description = "Export logs to BigQuery for long-term analysis"
  type        = bool
  default     = false
}
