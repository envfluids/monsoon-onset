# -----------------------------------------------------------------------------
# Compute Module Outputs
# -----------------------------------------------------------------------------

output "cloud_run_services" {
  description = "Map of Cloud Run job names and resource IDs"
  value = {
    for key, job in google_cloud_run_v2_job.pipeline_jobs : key => {
      name = job.name
      id   = job.id
    }
  }
}

output "batch_job_template" {
  description = "Cloud Batch job template configuration for AIFS and TPU"
  value = {
    project       = var.project_id
    region        = var.region
    machine_type  = "n1-standard-8"
    gpu_type      = var.gpu_type
    gpu_count     = 1
    preemptible   = var.use_preemptible_gpu
    image         = var.aifs_image
    vpc_network   = var.vpc_id
    vpc_subnet    = var.vpc_subnetwork
    tpu_type        = var.tpu_type
    neuralgcm_image = var.neuralgcm_image
  }
}

output "tpu_config" {
  description = "TPU configuration for NeuralGCM"
  value       = local.tpu_config
}

output "tpu_service_account_email" {
  description = "TPU service account email"
  value       = google_service_account.tpu.email
}

output "tpu_service_account_id" {
  description = "TPU service account resource ID"
  value       = google_service_account.tpu.name
}
