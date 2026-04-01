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
