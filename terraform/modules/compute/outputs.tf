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
    project           = var.project_id
    region            = var.region
    machine_type      = local.gpu_machine_type
    gpu_type          = var.gpu_type
    gpu_count         = 1
    os_image          = var.batch_vm_os_image
    boot_disk_gb      = var.batch_boot_disk_size_gb
    boot_disk_type    = var.batch_boot_disk_type
    model_resources   = local.batch_model_resources
    max_attempts      = var.batch_job_max_attempts
    image_streaming   = var.batch_enable_image_streaming
    preemptible       = var.use_preemptible_gpu
    aifs_v2_image     = var.aifs_v2_image
    aifs_ens_v2_image = var.aifs_ens_v2_image
    neuralgcm_image   = var.neuralgcm_image
    blend_image       = var.blend_image
    gencast_image     = var.gencast_image
    vpc_network       = var.vpc_id
    vpc_subnet        = var.vpc_subnetwork
  }
}

output "gencast_tpu_dispatch_template" {
  description = "TPU dispatch template for GenCast inference"
  value = {
    zone                   = var.gencast_tpu_zone
    accelerator_type       = var.gencast_tpu_accelerator_type
    runtime_version        = var.gencast_tpu_runtime_version
    spot                   = var.gencast_tpu_spot
    max_attempts           = var.gencast_tpu_max_attempts
    poll_interval_seconds  = var.gencast_tpu_poll_interval_seconds
    queue_timeout_seconds  = var.gencast_tpu_queue_timeout_seconds
    run_timeout_seconds    = var.gencast_tpu_run_timeout_seconds
    request_valid_duration = var.gencast_tpu_request_valid_duration
    workload_image         = var.gencast_image
    artifact_registry_host = split("/", var.gencast_image)[0]
    global_device_count    = var.gencast_tpu_global_device_count
    local_device_count     = var.gencast_tpu_local_device_count
    process_count          = var.gencast_tpu_process_count
    vpc_network            = var.vpc_id
    vpc_subnet             = var.tpu_vpc_subnetwork != "" ? var.tpu_vpc_subnetwork : var.vpc_subnetwork
  }
}
