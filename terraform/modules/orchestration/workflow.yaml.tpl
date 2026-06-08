# -----------------------------------------------------------------------------
# Monsoon Pipeline Workflow — Event-Advanced, Artifact-Driven
#
# Scheduled runs download missing ICs and submit currently-ready work. GCS
# finalization notifications for common-bucket intermediate markers invoke the
# same workflow, which probes pipeline-state and submits the next ready stages.
# -----------------------------------------------------------------------------

main:
  params: [args]
  steps:
    - init:
        assign:
          - requested_date: $${default(map.get(args, "date"), "")}
          - action: $${default(map.get(args, "action"), "run")}
          - event_type: $${default(map.get(args, "type"), "")}
          - event_data: $${default(map.get(args, "data"), json.decode("{}"))}
          - event_message: $${default(map.get(event_data, "message"), json.decode("{}"))}
          - event_payload: $${json.decode(base64.decode(default(map.get(event_message, "data"), "e30=")))}
          - event_object_name: $${default(map.get(event_payload, "name"), "")}
          - project_id: "${project_id}"
          - common_bucket: "${common_bucket}"
          - region_buckets: ${jsonencode(region_buckets)}
          - regions: ${jsonencode(regions)}
          - region_names: ${jsonencode([for r, _ in regions : r])}
          - pipeline_state_url: "${pipeline_state_url}"
          - default_ic_download:
              source: ""
              date: ""
              missing: []

    - maybe_advance_from_event:
        switch:
          - condition: $${event_type != "" and text.match_regex(event_object_name, "^intermediate/.*_done$")}
            next: advance_probe_state
          - condition: $${event_type != ""}
            next: return_ignored_event
          - condition: true
            next: probe_state_initial

    - advance_probe_state:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: ""
        result: advance_state

    - advance_submit_ready_work:
        call: submit_ready_work
        args:
          state: $${advance_state}
          common_bucket: $${common_bucket}
          region_buckets: $${region_buckets}
          regions: $${regions}
          region_names: $${region_names}

    - return_advanced:
        return:
          status: "advanced"
          marker: $${event_object_name}
          state: $${advance_state}

    - return_ignored_event:
        return:
          status: "ignored_event"
          object: $${event_object_name}

    - probe_state_initial:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state

    - maybe_return_checked:
        switch:
          - condition: $${action == "check"}
            next: return_checked
          - condition: true
            next: log_initial_plan

    - log_initial_plan:
        call: sys.log
        args:
          severity: INFO
          text: $${"initial actions=" + json.encode_to_string(state.actions)}

    # -------------------------------------------------------------------------
    # 1. Download missing ICs by source. Downloader completion writes
    #    intermediate/latest_* markers that can independently advance the
    #    pipeline through the Eventarc/Pub/Sub path.
    # -------------------------------------------------------------------------
    - download_ics:
        parallel:
          branches:
            - download_ecmwf:
                steps:
                  - load_ecmwf_download:
                      assign:
                        - ecmwf_download: $${default(map.get(state.actions.ic_to_download_by_source, "ecmwf"), default_ic_download)}
                  - maybe_download_ecmwf:
                      switch:
                        - condition: $${ecmwf_download.date == ""}
                          next: ecmwf_done
                        - condition: true
                          next: run_download_ecmwf
                  - run_download_ecmwf:
                      try:
                        call: googleapis.run.v2.projects.locations.jobs.run
                        args:
                          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.downloader.name}"
                          body:
                            overrides:
                              containerOverrides:
                                - env:
                                    - name: SOURCE
                                      value: "ecmwf"
                                    - name: DATE
                                      value: $${ecmwf_download.date}
                      except:
                        as: e
                        steps:
                          - log_ecmwf_download_failed:
                              call: sys.log
                              args:
                                severity: ERROR
                                text: '$${"ecmwf download failed for " + ecmwf_download.date + ": " + json.encode_to_string(e)}'
                  - ecmwf_done:
                      assign:
                        - ecmwf_branch_done: true
            - download_ncep:
                steps:
                  - load_ncep_download:
                      assign:
                        - ncep_download: $${default(map.get(state.actions.ic_to_download_by_source, "ncep"), default_ic_download)}
                  - maybe_download_ncep:
                      switch:
                        - condition: $${ncep_download.date == ""}
                          next: ncep_done
                        - condition: true
                          next: run_download_ncep
                  - run_download_ncep:
                      try:
                        call: googleapis.run.v2.projects.locations.jobs.run
                        args:
                          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.downloader.name}"
                          body:
                            overrides:
                              containerOverrides:
                                - env:
                                    - name: SOURCE
                                      value: "ncep"
                                    - name: DATE
                                      value: $${ncep_download.date}
                      except:
                        as: e
                        steps:
                          - log_ncep_download_failed:
                              call: sys.log
                              args:
                                severity: ERROR
                                text: '$${"ncep download failed for " + ncep_download.date + ": " + json.encode_to_string(e)}'
                  - ncep_done:
                      assign:
                        - ncep_branch_done: true

    - probe_state_post_ic:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state_post_ic

    - log_post_ic_plan:
        call: sys.log
        args:
          severity: INFO
          text: $${"post-ic actions=" + json.encode_to_string(state_post_ic.actions)}

    - submit_ready_work_after_ic:
        call: submit_ready_work
        args:
          state: $${state_post_ic}
          common_bucket: $${common_bucket}
          region_buckets: $${region_buckets}
          regions: $${regions}
          region_names: $${region_names}

    - return_submitted:
        return:
          status: "submitted"
          state: $${state_post_ic}

    - return_checked:
        return:
          status: "checked"
          state: $${state}


# -----------------------------------------------------------------------------
# Subroutines
# -----------------------------------------------------------------------------

submit_ready_work:
  params: [state, common_bucket, region_buckets, regions, region_names]
  steps:
    - init_ready_work:
        assign:
          - actions: $${state.actions}
          - default_model_action:
              model: ""
              date: ""
              regions: []
          - default_region_action:
              region: ""
              date: ""
              blends: []
              job_suffix: ""
              fingerprint: ""
              items: []

    - submit_batch_models:
        parallel:
          branches:
            - batch_model_noop:
                steps:
                  - batch_model_noop_done:
                      assign:
                        - batch_model_noop_branch_done: true
%{ for model in batch_models_in_use ~}
            - submit_${model}:
                steps:
                  - load_${model}_action:
                      assign:
                        - ${model}_action: $${default(map.get(actions.models_to_run_by_model, "${model}"), default_model_action)}
                  - maybe_submit_${model}:
                      switch:
                        - condition: $${${model}_action.date == ""}
                          next: ${model}_submit_done
                        - condition: true
                          next: submit_${model}_batch
                  - submit_${model}_batch:
                      call: submit_batch_stage
                      args:
                        job_id: $${"${replace(lower(model), "_", "-")}-" + text.replace_all(${model}_action.date, "T", "-")}
                        image: "${model_images[model]}"
                        machine_type: "${batch_config.model_resources[model].machine_type}"
                        cpu_milli: ${batch_config.model_resources[model].cpu_milli}
                        memory_mib: ${batch_config.model_resources[model].memory_mib}
                        boot_disk_size_gb: ${batch_config.model_resources[model].boot_disk_size_gb}
                        boot_disk_type: "${batch_config.model_resources[model].boot_disk_type}"
                        install_gpu_drivers: ${batch_config.model_resources[model].install_gpu_drivers}
                        provisioning_model: "${batch_config.model_resources[model].provisioning_model}"
                        max_run_duration: "${batch_config.model_resources[model].max_run_duration}"
                        accelerators: ${batch_config.model_resources[model].gpu_type != null && batch_config.model_resources[model].gpu_count != null ? jsonencode([{ type = batch_config.model_resources[model].gpu_type, count = batch_config.model_resources[model].gpu_count }]) : "[]"}
                        volumes: ${batch_config.model_resources[model].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common" }]) : "[]"}
                        env_vars:
                          DATE: $${${model}_action.date}
                          MODEL: "${model}"
                          FORECAST_REGIONS: $${json.encode_to_string(${model}_action.regions)}
                          GCS_COMMON_BUCKET: $${common_bucket}
                          GCS_REGION_BUCKETS: $${json.encode_to_string(region_buckets)}
                          REGION_MODELS: '${jsonencode({ for k, v in regions : k => v.models })}'
                          REGIONS: '${jsonencode(regions)}'
                          PROJECT_ID: "${project_id}"
                          UPLOAD_FULL_FIELD: "${contains(full_field_models, model) ? "true" : "false"}"
                  - ${model}_submit_done:
                      assign:
                        - ${model}_submit_branch_done: true
%{ endfor ~}

%{ if contains(models_in_use, "gencast") ~}
    - submit_gencast:
        steps:
          - load_gencast_action:
              assign:
                - gencast_action: $${default(map.get(actions.models_to_run_by_model, "gencast"), default_model_action)}
          - maybe_submit_gencast:
              switch:
                - condition: $${gencast_action.date == ""}
                  next: gencast_submit_done
                - condition: true
                  next: run_gencast_dispatch
          - run_gencast_dispatch:
              try:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs["tpu-dispatch"].name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: WORKLOAD_NAME
                              value: "gencast"
                            - name: RUN_ID
                              value: $${"gencast-" + text.replace_all(gencast_action.date, "T", "-")}
                            - name: DATE
                              value: $${gencast_action.date}
                            - name: WORKLOAD_IMAGE
                              value: "${tpu_config.workload_image}"
                            - name: FORECAST_REGIONS
                              value: $${json.encode_to_string(gencast_action.regions)}
                            - name: GCS_COMMON_BUCKET
                              value: $${common_bucket}
                            - name: GCS_REGION_BUCKETS
                              value: $${json.encode_to_string(region_buckets)}
                            - name: PROJECT_ID
                              value: "${project_id}"
                            - name: TPU_ZONE
                              value: "${tpu_config.zone}"
                            - name: TPU_ACCELERATOR_TYPE
                              value: "${tpu_config.accelerator_type}"
                            - name: TPU_RUNTIME_VERSION
                              value: "${tpu_config.runtime_version}"
                            - name: TPU_SPOT
                              value: "${tpu_config.spot}"
                            - name: TPU_NETWORK
                              value: "${tpu_config.vpc_network}"
                            - name: TPU_SUBNETWORK
                              value: "${tpu_config.vpc_subnet}"
                            - name: TPU_SERVICE_ACCOUNT
                              value: "${pipeline_sa}"
                            - name: ARTIFACT_REGISTRY_HOST
                              value: "${tpu_config.artifact_registry_host}"
                            - name: MAX_ATTEMPTS
                              value: "${tpu_config.max_attempts}"
                            - name: POLL_INTERVAL_SECONDS
                              value: "${tpu_config.poll_interval_seconds}"
                            - name: QUEUE_TIMEOUT_SECONDS
                              value: "${tpu_config.queue_timeout_seconds}"
                            - name: RUN_TIMEOUT_SECONDS
                              value: "${tpu_config.run_timeout_seconds}"
                            - name: REQUEST_VALID_DURATION
                              value: "${tpu_config.request_valid_duration}"
                            - name: GENCAST_EXPECTED_GLOBAL_DEVICES
                              value: "${tpu_config.global_device_count}"
                            - name: GENCAST_EXPECTED_LOCAL_DEVICES
                              value: "${tpu_config.local_device_count}"
                            - name: GENCAST_EXPECTED_PROCESS_COUNT
                              value: "${tpu_config.process_count}"
                            - name: GENCAST_ENSEMBLE_MEMBERS
                              value: "${tpu_config.global_device_count}"
                  connector_params:
                    skip_polling: true
              except:
                as: e
                steps:
                  - log_gencast_dispatch_error:
                      call: sys.log
                      args:
                        severity: WARNING
                        text: '$${"gencast dispatch submission returned: " + json.encode_to_string(e)}'
          - gencast_submit_done:
              assign:
                - gencast_submit_complete: true
%{ endif ~}

    - submit_blends:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_blend_action:
                assign:
                  - blend_action: $${default(map.get(actions.regions_to_blend_by_region, region_name), default_region_action)}
            - maybe_submit_blend:
                switch:
                  - condition: $${blend_action.date == ""}
                    next: end_blend_iteration
                  - condition: true
                    next: submit_blend_batch
            - submit_blend_batch:
                call: submit_batch_stage
                args:
                  job_id: $${"blend-" + region_name + "-" + text.replace_all(blend_action.date, "T", "-") + "-" + blend_action.job_suffix}
                  image: "${model_images["blend"]}"
                  machine_type: "${batch_config.model_resources["blend"].machine_type}"
                  cpu_milli: ${batch_config.model_resources["blend"].cpu_milli}
                  memory_mib: ${batch_config.model_resources["blend"].memory_mib}
                  boot_disk_size_gb: ${batch_config.model_resources["blend"].boot_disk_size_gb}
                  boot_disk_type: "${batch_config.model_resources["blend"].boot_disk_type}"
                  install_gpu_drivers: ${batch_config.model_resources["blend"].install_gpu_drivers}
                  provisioning_model: "${batch_config.model_resources["blend"].provisioning_model}"
                  max_run_duration: "${batch_config.model_resources["blend"].max_run_duration}"
                  accelerators: []
                  volumes: ${batch_config.model_resources["blend"].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common" }]) : "[]"}
                  env_vars:
                    DATE: $${blend_action.date}
                    FORECAST_REGION: $${region_name}
                    RUN_MODE: "blend"
                    BLEND_NAMES: $${json.encode_to_string(blend_action.blends)}
                    GCS_COMMON_BUCKET: $${common_bucket}
                    GCS_REGION_BUCKETS: $${json.encode_to_string(region_buckets)}
                    REGION_MODELS: '${jsonencode({ for k, v in regions : k => v.models })}'
                    REGIONS: '${jsonencode(regions)}'
                    PROJECT_ID: "${project_id}"
            - end_blend_iteration:
                assign:
                  - last_blend_region_checked: $${region_name}

    - submit_diagnostics:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_diagnostics_action:
                assign:
                  - diagnostics_action: $${default(map.get(actions.regions_to_diagnose_by_region, region_name), default_region_action)}
            - maybe_submit_diagnostics:
                switch:
                  - condition: $${diagnostics_action.date == ""}
                    next: end_diagnostics_iteration
                  - condition: true
                    next: submit_diagnostics_batch
            - submit_diagnostics_batch:
                call: submit_batch_stage
                args:
                  job_id: $${"diagnostics-" + region_name + "-" + text.replace_all(diagnostics_action.date, "T", "-") + "-" + diagnostics_action.job_suffix}
                  image: "${model_images["blend"]}"
                  machine_type: "${batch_config.model_resources["diagnostics"].machine_type}"
                  cpu_milli: ${batch_config.model_resources["diagnostics"].cpu_milli}
                  memory_mib: ${batch_config.model_resources["diagnostics"].memory_mib}
                  boot_disk_size_gb: ${batch_config.model_resources["diagnostics"].boot_disk_size_gb}
                  boot_disk_type: "${batch_config.model_resources["diagnostics"].boot_disk_type}"
                  install_gpu_drivers: ${batch_config.model_resources["diagnostics"].install_gpu_drivers}
                  provisioning_model: "${batch_config.model_resources["diagnostics"].provisioning_model}"
                  max_run_duration: "${batch_config.model_resources["diagnostics"].max_run_duration}"
                  accelerators: []
                  volumes: ${batch_config.model_resources["diagnostics"].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common" }]) : "[]"}
                  env_vars:
                    DATE: $${diagnostics_action.date}
                    FORECAST_REGION: $${region_name}
                    RUN_MODE: "diagnostics"
                    BLEND_NAMES: $${json.encode_to_string(diagnostics_action.blends)}
                    GCS_COMMON_BUCKET: $${common_bucket}
                    GCS_REGION_BUCKETS: $${json.encode_to_string(region_buckets)}
                    REGION_MODELS: '${jsonencode({ for k, v in regions : k => v.models })}'
                    REGIONS: '${jsonencode(regions)}'
                    PROJECT_ID: "${project_id}"
            - end_diagnostics_iteration:
                assign:
                  - last_diagnostics_region_checked: $${region_name}

    - submit_sync:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_sync_inputs:
                assign:
                  - region_cfg: $${map.get(regions, region_name)}
                  - sync_action: $${default(map.get(actions.regions_to_sync_by_region, region_name), default_region_action)}
            - maybe_submit_sync:
                switch:
                  - condition: $${sync_action.date == ""}
                    next: end_sync_iteration
                  - condition: true
                    next: run_sync
            - run_sync:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.sync.name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: DATE
                              value: $${sync_action.date}
                            - name: FORECAST_REGION
                              value: $${region_name}
                            - name: GCS_REGION_BUCKETS
                              value: $${json.encode_to_string(region_buckets)}
                            - name: SYNC_SPEC
                              value: $${json.encode_to_string(region_cfg.sync)}
                            - name: SYNC_FINGERPRINT
                              value: $${sync_action.fingerprint}
                            - name: SYNC_ITEMS
                              value: $${json.encode_to_string(sync_action.items)}
            - end_sync_iteration:
                assign:
                  - last_sync_region_checked: $${region_name}

    - ready_work_done:
        return: "submitted"


submit_batch_stage:
  params:
    - job_id
    - image
    - machine_type
    - cpu_milli
    - memory_mib
    - boot_disk_size_gb
    - boot_disk_type
    - install_gpu_drivers
    - provisioning_model
    - max_run_duration
    - accelerators
    - volumes
    - env_vars
  steps:
    - init_batch_submit:
        assign:
          - job_name: $${"projects/${project_id}/locations/${region}/jobs/" + job_id}
          - recreate_attempted: false
    - create_batch_job:
        try:
          call: http.post
          args:
            url: $${"https://batch.googleapis.com/v1/projects/${project_id}/locations/${region}/jobs?jobId=" + job_id}
            auth:
              type: OAuth2
            body:
              taskGroups:
                - taskCount: 1
                  taskSpec:
                    computeResource:
                      cpuMilli: $${cpu_milli}
                      memoryMib: $${memory_mib}
                    maxRetryCount: 3
                    maxRunDuration: $${max_run_duration}
                    runnables:
                      - container:
                          imageUri: $${image}
                          enableImageStreaming: ${batch_config.image_streaming}
                        environment:
                          variables: $${env_vars}
                    volumes: $${volumes}
              allocationPolicy:
                serviceAccount:
                  email: "${pipeline_sa}"
                instances:
                  - installGpuDrivers: $${install_gpu_drivers}
                    policy:
                      machineType: $${machine_type}
                      bootDisk:
                        image: "${batch_config.os_image}"
                        sizeGb: $${boot_disk_size_gb}
                        type: $${boot_disk_type}
                      provisioningModel: $${provisioning_model}
                      accelerators: $${accelerators}
                location:
                  allowedLocations: ["regions/${region}"]
                network:
                  networkInterfaces:
                    - network: "${batch_config.vpc_network}"
                      subnetwork: "${batch_config.vpc_subnet}"
                      noExternalIpAddress: true
              logsPolicy:
                destination: CLOUD_LOGGING
          result: create_batch_response
        except:
          as: e
          steps:
            - handle_batch_create_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 409}
                    next: get_existing_batch_job
                  - condition: true
                    raise: $${e}
    - batch_job_created:
        return: "created"

    - get_existing_batch_job:
        try:
          call: googleapis.batch.v1.projects.locations.jobs.get
          args:
            name: $${job_name}
          result: existing_batch_job
        except:
          as: e
          steps:
            - handle_existing_batch_get_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 404}
                    next: create_batch_job
                  - condition: true
                    raise: $${e}

    - check_existing_batch_job:
        switch:
          - condition: $${existing_batch_job.status.state == "FAILED" or existing_batch_job.status.state == "CANCELLED" or existing_batch_job.status.state == "SUCCEEDED"}
            next: maybe_recreate_existing_batch_job
          - condition: true
            next: batch_job_already_exists

    - maybe_recreate_existing_batch_job:
        switch:
          - condition: $${recreate_attempted}
            next: stale_batch_job_delete_pending
          - condition: true
            next: delete_existing_batch_job

    - delete_existing_batch_job:
        call: googleapis.batch.v1.projects.locations.jobs.delete
        args:
          name: $${job_name}

    - mark_recreate_attempted:
        assign:
          - recreate_attempted: true

    - wait_for_batch_delete:
        call: sys.sleep
        args:
          seconds: 30
        next: create_batch_job

    - batch_job_already_exists:
        return: "exists"

    - stale_batch_job_delete_pending:
        return: "stale_delete_pending"


pipeline_state:
  params: [base_url, date]
  steps:
    - build_url:
        switch:
          - condition: $${date == ""}
            next: set_url_no_date
          - condition: true
            next: set_url_with_date
    - set_url_no_date:
        assign:
          - url: $${base_url + "/state"}
        next: fetch
    - set_url_with_date:
        assign:
          - url: $${base_url + "/state?date=" + date}
    - fetch:
        call: http.get
        args:
          url: $${url}
          auth:
            type: OIDC
        result: response
    - return_body:
        return: $${response.body}
