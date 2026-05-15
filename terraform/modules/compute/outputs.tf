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

output "pipeline_state_service_name" {
  description = "Cloud Run service name for the pipeline-state service"
  value       = google_cloud_run_v2_service.pipeline_state.name
}

output "pipeline_state_url" {
  description = "Cloud Run service URL for the pipeline-state service"
  value       = google_cloud_run_v2_service.pipeline_state.uri
}

output "batch_job_template" {
  description = "Cloud Batch job template configuration for model inference"
  value = {
    project         = var.project_id
    region          = var.region
    machine_type    = local.gpu_machine_type
    gpu_type        = var.gpu_type
    gpu_count       = 1
    os_image        = var.batch_vm_os_image
    boot_disk_gb    = var.batch_boot_disk_size_gb
    image_streaming = var.batch_enable_image_streaming
    preemptible     = var.use_preemptible_gpu
    aifs_image      = var.aifs_image
    neuralgcm_image = var.neuralgcm_image
    gencast_image   = var.gencast_image
    vpc_network     = var.vpc_id
    vpc_subnet      = var.vpc_subnetwork
  }
}
