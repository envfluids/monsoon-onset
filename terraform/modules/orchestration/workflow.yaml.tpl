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

%{ if delivery_check_marker != "" ~}
    - maybe_check_delivery:
        switch:
          - condition: $${action == "check_delivery"}
            next: check_delivery_init

%{ endif ~}
%{ if length(ensemble_ladder) > 0 ~}
    - maybe_check_ensemble:
        switch:
          - condition: $${action == "check_ensemble"}
            next: check_ensemble_init

%{ endif ~}
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
%{ if delivery_check_marker != "" ~}

    # -------------------------------------------------------------------------
    # Delivery deadline check (scheduled separately with action=check_delivery):
    # logs DELIVERY_LATE at WARNING when today's marker is missing, which the
    # delivery alert policy matches. Doesn't probe or submit anything.
    # -------------------------------------------------------------------------
    - check_delivery_init:
        assign:
          - delivery_now: $${time.format(sys.now())}
          - delivery_date: $${text.replace_all(text.substring(delivery_now, 0, 10), "-", "") + "T00"}
          - delivery_marker: $${text.replace_all("${delivery_check_marker}", "%DATE%", delivery_date)}
    - check_delivery_marker:
        try:
          call: googleapis.storage.v1.objects.get
          args:
            bucket: $${common_bucket}
            object: $${text.url_encode(delivery_marker)}
        except:
          as: e
          steps:
            - classify_delivery_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 404}
                    next: log_delivery_late
                  - condition: true
                    raise: $${e}
    - log_delivery_on_time:
        call: sys.log
        args:
          severity: INFO
          text: '$${"DELIVERY_OK gs://" + common_bucket + "/" + delivery_marker + " present at " + delivery_now}'
        next: return_delivery_checked
    - log_delivery_late:
        call: sys.log
        args:
          severity: WARNING
          text: '$${"DELIVERY_LATE gs://" + common_bucket + "/" + delivery_marker + " missing at " + delivery_now}'
    - return_delivery_checked:
        return:
          status: "delivery_checked"
          marker: $${delivery_marker}
%{ endif ~}
%{ if length(ensemble_ladder) > 0 ~}

    # -------------------------------------------------------------------------
    # Fallback-ladder check (scheduled every 2 min with action=check_ensemble):
    # starts the next fallback job for a Batch model that hasn't started, and
    # cancels the other jobs once one has claimed the date. Today's date unless
    # args.date is given. Never submits any other stage.
    # -------------------------------------------------------------------------
    - check_ensemble_init:
        assign:
          - ladder_date: $${text.replace_all(text.substring(time.format(sys.now()), 0, 10), "-", "") + "T00"}
    - maybe_requested_ladder_date:
        switch:
          - condition: $${requested_date != ""}
            steps:
              - use_requested_ladder_date:
                  assign:
                    - ladder_date: $${requested_date}
%{ for model in keys(ensemble_ladder) ~}
    - check_ladder_${model}:
        call: ensemble_ladder_${model}
        args:
          date: $${ladder_date}
          regions: []
          run_primary: false
        result: ladder_${model}_result
%{ endfor ~}
    - return_ensemble_checked:
        return:
          status: "ensemble_checked"
          date: $${ladder_date}
%{ endif ~}


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
%{ if contains(keys(ensemble_ladder), model) ~}
                  # Fallback ladder: resubmits only the job that owns the date.
                  - submit_${model}_batch:
                      call: ensemble_ladder_${model}
                      args:
                        date: $${${model}_action.date}
                        regions: $${${model}_action.regions}
                        run_primary: true
%{ else ~}
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
                        volumes: ${batch_config.model_resources[model].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common", mountOptions = batch_config.model_resources[model].gcs_mount_options }]) : "[]"}
                        max_attempts: ${try(batch_model_max_attempts[model], 0)}
                        extra_labels: {}
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
%{ for name, value in try(batch_model_env[model], {}) ~}
                          ${name}: ${jsonencode(value)}
%{ endfor ~}
%{ endif ~}
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
                  volumes: ${batch_config.model_resources["blend"].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common", mountOptions = batch_config.model_resources["blend"].gcs_mount_options }]) : "[]"}
                  max_attempts: 0
                  extra_labels: {}
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
                  volumes: ${batch_config.model_resources["diagnostics"].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common", mountOptions = batch_config.model_resources["diagnostics"].gcs_mount_options }]) : "[]"}
                  max_attempts: 0
                  extra_labels: {}
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
            # Fire and forget: a slow or failing sync (e.g. Google Drive) must never
            # hold up or abort the pass. The sync job records its own failures.
            - run_sync:
                try:
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
                    connector_params:
                      skip_polling: true
                except:
                  as: e
                  steps:
                    - log_sync_start_error:
                        call: sys.log
                        args:
                          severity: WARNING
                          text: '$${"sync job start failed for " + region_name + " " + sync_action.date + ": " + json.encode_to_string(e)}'
            - end_sync_iteration:
                assign:
                  - last_sync_region_checked: $${region_name}

%{ if contains(keys(cloud_run_jobs), "drywetcast") ~}
    - submit_drywetcast:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_drywetcast_action:
                assign:
                  - drywetcast_action: $${default(map.get(actions, ["regions_to_drywetcast_by_region", region_name]), default_region_action)}
            - maybe_run_drywetcast:
                switch:
                  - condition: $${drywetcast_action.date == ""}
                    next: end_drywetcast_iteration
                  - condition: true
                    next: run_drywetcast
            - run_drywetcast:
                try:
                  call: googleapis.run.v2.projects.locations.jobs.run
                  args:
                    name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.drywetcast.name}"
                    body:
                      overrides:
                        containerOverrides:
                          - env:
                              - name: DATE
                                value: $${drywetcast_action.date}
                              - name: FORECAST_REGION
                                value: $${region_name}
                              - name: DRYWETCAST_CONFIGS
                                value: $${json.encode_to_string(drywetcast_action.configs)}
                              - name: DRYWETCAST_FINALIZE
                                value: $${string(drywetcast_action.finalize_ncmrwf)}
                    connector_params:
                      skip_polling: true
                except:
                  as: e
                  steps:
                    - log_drywetcast_start_error:
                        call: sys.log
                        args:
                          severity: ERROR
                          text: '$${"drywetcast job start failed for " + region_name + " " + drywetcast_action.date + ": " + json.encode_to_string(e)}'
            - end_drywetcast_iteration:
                assign:
                  - last_drywetcast_region_checked: $${region_name}

%{ endif ~}
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
    - max_attempts  # attempts per job id before FAILED/CANCELLED stops being resubmitted; 0 = unlimited
    - extra_labels  # labels besides attempt ({} for none); no default: Workflows only takes literal scalars
    # Fallback-ladder jobs override these; the defaults are today's job.
    - batch_region: "${region}"
    - batch_subnet: "${batch_config.vpc_subnet}"
    - initial_attempt: 1
  steps:
    - init_batch_submit:
        assign:
          - job_name: $${"projects/${project_id}/locations/" + batch_region + "/jobs/" + job_id}
          - recreate_attempted: false
          - attempt: $${initial_attempt}
          - batch_labels: $${extra_labels}
          - batch_labels.attempt: $${string(attempt)}
    - create_batch_job:
        try:
          call: http.post
          args:
            url: $${"https://batch.googleapis.com/v1/projects/${project_id}/locations/" + batch_region + "/jobs?jobId=" + job_id}
            auth:
              type: OAuth2
            body:
              labels: $${batch_labels}
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
                  allowedLocations:
                    - $${"regions/" + batch_region}
                network:
                  networkInterfaces:
                    - network: "${batch_config.vpc_network}"
                      subnetwork: $${batch_subnet}
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
            next: count_previous_attempts

    # The attempt number travels on the job itself (label), so every pass sees
    # the same count. Jobs created before the label existed count as attempt 1.
    - count_previous_attempts:
        assign:
          - previous_attempt: $${int(default(map.get(existing_batch_job, ["labels", "attempt"]), "1"))}
    - enforce_attempt_cap:
        switch:
          - condition: $${max_attempts > 0 and existing_batch_job.status.state != "SUCCEEDED" and previous_attempt >= max_attempts}
            next: log_retry_cap_reached
          - condition: true
            next: recheck_before_delete

    # Re-read immediately before deleting: only delete the same job (uid) and only
    # while it is still finished, so a job another pass just recreated is kept.
    - recheck_before_delete:
        try:
          call: googleapis.batch.v1.projects.locations.jobs.get
          args:
            name: $${job_name}
          result: recheck_batch_job
        except:
          as: e
          steps:
            - handle_recheck_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 404}
                    next: mark_recreate_attempted  # already gone: count the attempt, then create
                  - condition: true
                    raise: $${e}
    - verify_same_finished_job:
        switch:
          - condition: $${recheck_batch_job.uid == existing_batch_job.uid and (recheck_batch_job.status.state == "FAILED" or recheck_batch_job.status.state == "CANCELLED" or recheck_batch_job.status.state == "SUCCEEDED")}
            next: delete_existing_batch_job
          - condition: true
            next: log_delete_skipped

    - delete_existing_batch_job:
        call: googleapis.batch.v1.projects.locations.jobs.delete
        args:
          name: $${job_name}

    - mark_recreate_attempted:
        assign:
          - recreate_attempted: true
          - attempt: $${previous_attempt + 1}
          - batch_labels.attempt: $${string(attempt)}

    - wait_for_batch_delete:
        call: sys.sleep
        args:
          seconds: 30
        next: create_batch_job

    - batch_job_already_exists:
        return: "exists"

    - stale_batch_job_delete_pending:
        return: "stale_delete_pending"

    - log_retry_cap_reached:
        call: sys.log
        args:
          severity: WARNING
          text: '$${"BATCH_RETRY_CAP " + job_id + " is " + existing_batch_job.status.state + " after " + string(previous_attempt) + " of " + string(max_attempts) + " attempts; not resubmitting"}'
    - batch_retry_cap_reached:
        return: "retry_cap_reached"

    - log_delete_skipped:
        call: sys.log
        args:
          severity: INFO
          text: '$${"Not deleting " + job_id + ": it changed since it was read (now uid " + recheck_batch_job.uid + ", state " + recheck_batch_job.status.state + ")"}'
    - batch_delete_skipped:
        return: "changed"


%{ for model, ladder in ensemble_ladder ~}
# -----------------------------------------------------------------------------
# Fallback ladder for ${model}: the primary job, then ${join(", ", [for step in ladder.steps : step.name])}.
# pipeline-state (/ensemble_ladder) decides; this subroutine acts. Each job
# claims the date when it starts (docker/aifs-ens-v2), so only the first job
# to start runs; the others are cancelled here.
# -----------------------------------------------------------------------------

ensemble_ladder_${model}:
  params: [date, regions, run_primary]
  steps:
    # A pipeline-state image without the endpoint (404, e.g. mid-rollout) means
    # no ladder: submit the primary exactly as before. Other errors raise.
    - init_ladder_response:
        assign:
          - ladder_response: null  # set in the except below too, so it must exist here
    - fetch_ladder:
        try:
          call: http.get
          args:
            url: $${"${pipeline_state_url}/ensemble_ladder?model=${model}&date=" + date}
            auth:
              type: OIDC
          result: ladder_response
        except:
          as: e
          steps:
            - classify_ladder_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) != 404}
                    raise: $${e}
            - log_ladder_unavailable:
                call: sys.log
                args:
                  severity: WARNING
                  text: '$${"ENSEMBLE_LADDER_UNAVAILABLE ${model} " + date + ": pipeline-state has no /ensemble_ladder; primary only"}'
            - no_ladder:
                assign:
                  - ladder_response:
                      body:
                        decision: "none"
                        reason: "ladder_unavailable"
                        winner_step: ""
                        ladder_active: false
                        log_won: false
    - read_ladder:
        assign:
          - ladder: $${ladder_response.body}
          - claim_prefix: $${"batch-state/${model}/" + date}
    - maybe_submit_owner:
        switch:
          - condition: $${run_primary and ladder.winner_step != ""}
            next: resubmit_ladder_winner
          - condition: $${run_primary and not ladder.ladder_active}
            next: submit_ladder_primary
          - condition: true
            next: act_on_ladder
    # The job that claimed the date is the only one resubmitted (retry cap as usual).
    - resubmit_ladder_winner:
        call: ladder_submit_${model}
        args:
          date: $${date}
          regions: $${regions}
          step: $${ladder.winner_step}
          attempt: $${int(ladder.primary_attempt)}
        result: owner_submit
        next: act_on_ladder
    - submit_ladder_primary:
        call: ladder_submit_${model}
        args:
          date: $${date}
          regions: $${regions}
          step: "primary"
          attempt: 1
        result: owner_submit
    - act_on_ladder:
        switch:
          - condition: $${ladder.decision == "submit"}
            next: maybe_force_ladder_claim
          - condition: $${ladder.decision == "cancel"}
            next: cancel_ladder_losers
          - condition: true
            next: maybe_log_ladder_won

    # Forced test runs (dev only): claim the date for the forced step before
    # it exists, so the primary stops at start and is cancelled.
    - maybe_force_ladder_claim:
        switch:
          - condition: $${ladder.force_claim}
            next: write_forced_ladder_claim
          - condition: true
            next: submit_ladder_step
    - write_forced_ladder_claim:
        try:
          call: googleapis.storage.v1.objects.insert
          args:
            bucket: "${common_bucket}"
            name: $${claim_prefix + "/winner"}
            ifGenerationMatch: 0
            uploadType: "media"
            body:
              job_id: $${map.get(ladder.jobs, ladder.step).job_id}
              forced: true
        except:
          as: e
          steps:
            - classify_forced_claim_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 412}
                    next: submit_ladder_step
                  - condition: true
                    raise: $${e}
    - submit_ladder_step:
        call: ladder_submit_${model}
        args:
          date: $${date}
          regions: $${ladder.regions}
          step: $${ladder.step}
          attempt: $${int(ladder.primary_attempt)}
        result: step_submit
    - maybe_log_step_submitted:
        switch:
          - condition: $${step_submit.status == "created"}
            next: log_ladder_step_submitted
          - condition: true
            next: ladder_done
    - log_ladder_step_submitted:
        call: sys.log
        args:
          severity: WARNING
          text: '$${"ENSEMBLE_FALLBACK_SUBMITTED ${model} " + date + ": " + step_submit.job_id + " (" + step_submit.machine_type + ", " + step_submit.region + "); primary " + ladder.jobs.primary.state + " for " + string(ladder.jobs.primary.waited_minutes) + " min (" + ladder.reason + ")"}'
        next: ladder_done

    # Re-read each loser and cancel it only if it is the same job (uid) and
    # still unfinished. A loser that starts anyway stops at its claim check.
    - cancel_ladder_losers:
        for:
          value: loser
          in: $${ladder.cancel}
          steps:
            # Set here so the except blocks below update them (an except block's
            # own variables are local), and every jump is a loop-level step.
            - reset_ladder_loser:
                assign:
                  - loser_job: null
                  - loser_cancel_failed: false
            - recheck_ladder_loser:
                try:
                  call: googleapis.batch.v1.projects.locations.jobs.get
                  args:
                    name: $${loser.name}
                  result: loser_job
                except:
                  as: e
                  steps:
                    - reraise_unless_loser_gone:
                        switch:
                          - condition: $${default(map.get(e, "code"), 0) != 404}
                            raise: $${e}
            - skip_gone_ladder_loser:
                switch:
                  - condition: $${loser_job == null}
                    next: continue
            - maybe_cancel_ladder_loser:
                switch:
                  - condition: $${loser_job.uid == loser.uid and (loser_job.status.state == "QUEUED" or loser_job.status.state == "SCHEDULED" or loser_job.status.state == "RUNNING")}
                    next: cancel_ladder_loser
                  - condition: true
                    next: continue
            - cancel_ladder_loser:
                try:
                  call: http.post
                  args:
                    url: $${"https://batch.googleapis.com/v1/" + loser.name + ":cancel"}
                    auth:
                      type: OAuth2
                    body: {}
                except:
                  as: e
                  steps:
                    - log_ladder_cancel_failed:
                        call: sys.log
                        args:
                          severity: WARNING
                          text: '$${"ENSEMBLE_LADDER_CANCEL_FAILED " + loser.name + ": " + json.encode_to_string(e)}'
                    - mark_ladder_cancel_failed:
                        assign:
                          - loser_cancel_failed: true
            - skip_failed_ladder_cancel:
                switch:
                  - condition: $${loser_cancel_failed}
                    next: continue
            - log_ladder_loser_cancelled:
                call: sys.log
                args:
                  severity: INFO
                  text: '$${"ENSEMBLE_LADDER_CANCELLED " + loser.name + " (" + loser.step + "); " + ladder.reason + " " + ladder.winner}'

    # One ENSEMBLE_FALLBACK_WON per date: the create-only marker picks the pass.
    - maybe_log_ladder_won:
        switch:
          - condition: $${ladder.log_won}
            next: mark_ladder_won_logged
          - condition: true
            next: ladder_done
    - mark_ladder_won_logged:
        try:
          call: googleapis.storage.v1.objects.insert
          args:
            bucket: "${common_bucket}"
            name: $${claim_prefix + "/won_logged"}
            ifGenerationMatch: 0
            uploadType: "media"
            body:
              winner: $${ladder.winner}
        except:
          as: e
          steps:
            - classify_won_logged_error:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 412}
                    next: ladder_done
                  - condition: true
                    raise: $${e}
    - log_ladder_won:
        call: sys.log
        args:
          severity: WARNING
          text: '$${"ENSEMBLE_FALLBACK_WON ${model} " + date + ": " + ladder.winner + " (" + ladder.winner_step + ") started first; the other ladder jobs are cancelled"}'
    - ladder_done:
        return: $${ladder.decision}


ladder_submit_${model}:
  params: [date, regions, step, attempt]
  steps:
    - ladder_job_ids:
        assign:
          - primary_job_id: $${"${replace(lower(model), "_", "-")}-" + text.replace_all(date, "T", "-")}
          - claim_object: $${"batch-state/${model}/" + date + "/winner"}
    - pick_ladder_step:
        switch:
          - condition: $${step == "primary"}
            next: submit_ladder_job_primary
%{ for step in ladder.steps ~}
          - condition: $${step == "${step.name}"}
            next: submit_ladder_job_${step.name}
%{ endfor ~}
          - condition: true
            raise: $${"unknown ladder step " + step}
    - submit_ladder_job_primary:
        call: submit_batch_stage
        args:
          job_id: $${primary_job_id}
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
          volumes: ${batch_config.model_resources[model].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common", mountOptions = batch_config.model_resources[model].gcs_mount_options }]) : "[]"}
          max_attempts: ${try(batch_model_max_attempts[model], 0)}
          initial_attempt: $${attempt}
          extra_labels: {}
          env_vars:
            DATE: $${date}
            MODEL: "${model}"
            FORECAST_REGIONS: $${json.encode_to_string(regions)}
            GCS_COMMON_BUCKET: "${common_bucket}"
            GCS_REGION_BUCKETS: '${jsonencode(region_buckets)}'
            REGION_MODELS: '${jsonencode({ for k, v in regions : k => v.models })}'
            REGIONS: '${jsonencode(regions)}'
            PROJECT_ID: "${project_id}"
            UPLOAD_FULL_FIELD: "${contains(full_field_models, model) ? "true" : "false"}"
            ENSEMBLE_CLAIM_OBJECT: $${claim_object}
            LADDER_JOB_ID: $${primary_job_id}
%{ for name, value in try(batch_model_env[model], {}) ~}
            ${name}: ${jsonencode(value)}
%{ endfor ~}
        result: submit_status
    - return_ladder_job_primary:
        return:
          status: $${submit_status}
          job_id: $${primary_job_id}
          region: "${region}"
          machine_type: "${batch_config.model_resources[model].machine_type}"
%{ for step in ladder.steps ~}
    - submit_ladder_job_${step.name}:
        call: submit_batch_stage
        args:
          job_id: $${primary_job_id + "-${step.name}"}
          image: "${model_images[model]}"
          machine_type: "${step.machine_type}"
          cpu_milli: ${step.cpu_milli}
          memory_mib: ${step.memory_mib}
          boot_disk_size_gb: ${batch_config.model_resources[model].boot_disk_size_gb}
          boot_disk_type: "${batch_config.model_resources[model].boot_disk_type}"
          install_gpu_drivers: true
          provisioning_model: "${batch_config.model_resources[model].provisioning_model}"
          max_run_duration: "${step.max_run_duration}"
          accelerators: ${jsonencode([{ type = batch_config.model_resources[model].gpu_type, count = step.gpu_count }])}
          volumes: ${step.upload_full_field && batch_config.model_resources[model].mount_common_bucket ? jsonencode([{ gcs = { remotePath = common_bucket }, mountPath = "/mnt/disks/common", mountOptions = batch_config.model_resources[model].gcs_mount_options }]) : "[]"}
          max_attempts: ${try(batch_model_max_attempts[model], 0)}
          batch_region: "${step.region}"
          batch_subnet: "${step.subnet}"
          initial_attempt: $${attempt}
          extra_labels:
            ladder-step: "${step.name}"
            ladder-of: $${primary_job_id}
          env_vars:
            DATE: $${date}
            MODEL: "${model}"
            FORECAST_REGIONS: $${json.encode_to_string(regions)}
            GCS_COMMON_BUCKET: "${common_bucket}"
            GCS_REGION_BUCKETS: '${jsonencode(region_buckets)}'
            REGION_MODELS: '${jsonencode({ for k, v in regions : k => v.models })}'
            REGIONS: '${jsonencode(regions)}'
            PROJECT_ID: "${project_id}"
            UPLOAD_FULL_FIELD: "${step.upload_full_field && contains(full_field_models, model) ? "true" : "false"}"
            ENSEMBLE_CLAIM_OBJECT: $${claim_object}
            LADDER_JOB_ID: $${primary_job_id + "-${step.name}"}
%{ for name, value in try(batch_model_env[model], {}) ~}
            ${name}: ${jsonencode(value)}
%{ endfor ~}
        result: submit_status
    - return_ladder_job_${step.name}:
        return:
          status: $${submit_status}
          job_id: $${primary_job_id + "-${step.name}"}
          region: "${step.region}"
          machine_type: "${step.machine_type}"
%{ endfor ~}


%{ endfor ~}
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
