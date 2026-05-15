# -----------------------------------------------------------------------------
# Monsoon Pipeline Workflow — Greedy, IC-Driven, Deduplicated
#
# Each run probes pipeline-state, downloads missing ICs by source/date, greedily
# runs models whose IC source is present, then blends/syncs regions whose
# expected inputs are present. GCS artifacts are the success signal.
# -----------------------------------------------------------------------------

main:
  params: [args]
  steps:
    - init:
        assign:
          - requested_date: $${default(map.get(args, "date"), "")}
          - action: $${default(map.get(args, "action"), "run")}
          - project_id: "${project_id}"
          - common_bucket: "${common_bucket}"
          - region_buckets: ${jsonencode(region_buckets)}
          - regions: ${jsonencode(regions)}
          - region_names: ${jsonencode([for r, _ in regions : r])}
          - regions_by_model: ${jsonencode(regions_by_model)}
          - pipeline_state_url: "${pipeline_state_url}"
          - default_ic_download:
              source: ""
              date: ""
              missing: []
          - default_model_action:
              model: ""
              date: ""
              regions: []
          - default_region_action:
              region: ""
              date: ""

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
    # 1. Download missing ICs by source. A failed source logs an error and the
    #    post-download state probe will keep dependent model work blocked.
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

    # -------------------------------------------------------------------------
    # 2. Run each model at most once per IC date. Duplicate Batch job IDs mean
    #    another workflow already started the same model/date, so poll it.
    #    GenCast is launched as a TPU queued resource; GPU models stay on Batch.
    # -------------------------------------------------------------------------
    - run_models:
        parallel:
          branches:
            - run_model_noop:
                steps:
                  - model_noop_done:
                      assign:
                        - model_noop_branch_done: true
%{ for model in gpu_models_in_use ~}
            - run_${model}:
                steps:
                  - load_${model}_action:
                      assign:
                        - ${model}_action: $${default(map.get(state_post_ic.actions.models_to_run_by_model, "${model}"), default_model_action)}
                  - maybe_skip_${model}:
                      switch:
                        - condition: $${${model}_action.date == ""}
                          next: ${model}_done
                        - condition: true
                          next: prepare_${model}_batch
                  - prepare_${model}_batch:
                      assign:
                        - ${model}_job_id: $${"${replace(model, "_", "-")}-" + text.replace_all(${model}_action.date, "T", "-")}
                        - ${model}_job_name: $${"projects/" + project_id + "/locations/${region}/jobs/" + ${model}_job_id}
                  # A prior workflow run may have left a FAILED/CANCELLED job at
                  # this id. Batch ids are immutable, so resubmitting would 409
                  # forever. Probe first; if stale, delete (synchronous via the
                  # default connector polling) and then fall through to submit.
                  - probe_existing_${model}_job:
                      try:
                        call: googleapis.batch.v1.projects.locations.jobs.get
                        args:
                          name: $${${model}_job_name}
                        result: ${model}_existing
                      except:
                        as: e
                        steps:
                          - handle_${model}_probe_error:
                              switch:
                                - condition: $${default(map.get(e, "code"), 0) == 404}
                                  next: submit_${model}_batch
                                - condition: true
                                  raise: $${e}
                  - check_${model}_stale:
                      switch:
                        - condition: $${${model}_existing.status.state == "FAILED" or ${model}_existing.status.state == "CANCELLED"}
                          next: delete_stale_${model}_job
                        - condition: true
                          next: submit_${model}_batch
                  - delete_stale_${model}_job:
                      call: googleapis.batch.v1.projects.locations.jobs.delete
                      args:
                        name: $${${model}_job_name}
                      next: submit_${model}_batch
                  - submit_${model}_batch:
                      try:
                        call: googleapis.batch.v1.projects.locations.jobs.create
                        args:
                          parent: $${"projects/" + project_id + "/locations/${region}"}
                          jobId: $${${model}_job_id}
                          body:
                            taskGroups:
                              - taskCount: 1
                                taskSpec:
                                  computeResource:
                                    cpuMilli: 8000
                                    memoryMib: ${model == "aifs_ens" || model == "neuralgcm" ? 65536 : 32768}
                                  maxRetryCount: 1
                                  maxRunDuration: "${model == "aifs_ens" ? "7200s" : (model == "neuralgcm" ? "3600s" : "1800s")}"
                                  runnables:
                                    - container:
                                        imageUri: "${model_images[model]}"
                                        enableImageStreaming: ${batch_config.image_streaming}
                                      environment:
                                        variables:
                                          DATE: $${${model}_action.date}
                                          MODEL: "${model}"
                                          FORECAST_REGIONS: $${json.encode_to_string(${model}_action.regions)}
                                          GCS_COMMON_BUCKET: $${common_bucket}
                                          GCS_REGION_BUCKETS: $${json.encode_to_string(region_buckets)}
                                          UPLOAD_FULL_FIELD: "${contains(full_field_models, model) ? "true" : "false"}"
                            allocationPolicy:
                              serviceAccount:
                                email: "${pipeline_sa}"
                              instances:
                                - installGpuDrivers: true
                                  policy:
                                    machineType: "${batch_config.machine_type}"
                                    bootDisk:
                                      image: "${batch_config.os_image}"
                                      sizeGb: ${model == "aifs_ens" ? 200 : batch_config.boot_disk_gb}
                                    provisioningModel: '${batch_config.preemptible ? "SPOT" : "STANDARD"}'
                                    accelerators:
                                      - type: "${batch_config.gpu_type}"
                                        count: ${batch_config.gpu_count}
                              location:
                                allowedLocations: ["regions/${region}"]
                              network:
                                networkInterfaces:
                                  - network: "${batch_config.vpc_network}"
                                    subnetwork: "${batch_config.vpc_subnet}"
                                    noExternalIpAddress: true
                            logsPolicy:
                              destination: CLOUD_LOGGING
                          connector_params:
                            skip_polling: true
                      except:
                        as: e
                        steps:
                          - handle_${model}_submit_error:
                              switch:
                                - condition: $${default(map.get(e, "code"), 0) == 409}
                                  next: poll_${model}
                                - condition: true
                                  raise: $${e}
                  - poll_${model}:
                      steps:
                        - get_${model}_status:
                            call: googleapis.batch.v1.projects.locations.jobs.get
                            args:
                              name: $${${model}_job_name}
                            result: ${model}_status
                        - check_${model}_state:
                            switch:
                              - condition: $${${model}_status.status.state == "SUCCEEDED"}
                                next: ${model}_done
                              - condition: $${${model}_status.status.state == "FAILED" or ${model}_status.status.state == "CANCELLED"}
                                raise: '$${"${model} job did not complete successfully: " + ${model}_job_name + " state=" + ${model}_status.status.state}'
                        - sleep_${model}:
                            call: sys.sleep
                            args:
                              seconds: ${model == "neuralgcm" ? 120 : 60}
                            next: get_${model}_status
                  - ${model}_done:
                      assign:
                        - ${model}_branch_done: true
%{ endfor ~}
%{ if contains(models_in_use, "gencast") ~}
            - run_gencast:
                steps:
                  - load_gencast_action:
                      assign:
                        - gencast_action: $${default(map.get(state_post_ic.actions.models_to_run_by_model, "gencast"), default_model_action)}
                  - maybe_skip_gencast:
                      switch:
                        - condition: $${gencast_action.date == ""}
                          next: gencast_done
                        - condition: true
                          next: prepare_gencast_tpu
                  - prepare_gencast_tpu:
                      assign:
                        - gencast_qr_id: $${"gencast-" + text.replace_all(gencast_action.date, "T", "-")}
                        - gencast_node_id: $${gencast_qr_id}
                        - gencast_qr_url: $${"https://tpu.googleapis.com/v2alpha1/projects/" + project_id + "/locations/${tpu_config.zone}/queuedResources/" + gencast_qr_id}
                        - gencast_create_url: $${"https://tpu.googleapis.com/v2alpha1/projects/" + project_id + "/locations/${tpu_config.zone}/queuedResources?queuedResourceId=" + gencast_qr_id}
                        - gencast_poll_count: 0
                  - submit_gencast_tpu:
                      try:
                        call: http.post
                        args:
                          url: $${gencast_create_url}
                          auth:
                            type: OAuth2
                            scopes: ["https://www.googleapis.com/auth/cloud-platform"]
                          body:
                            queueingPolicy:
                              validUntilDuration: "${tpu_config.request_valid_duration}"
                            tpu:
                              nodeSpec:
                                - parent: "projects/${project_id}/locations/${tpu_config.zone}"
                                  nodeId: $${gencast_node_id}
                                  node:
                                    runtimeVersion: "${tpu_config.runtime_version}"
                                    acceleratorConfig:
                                      type: "${tpu_config.accelerator_type}"
                                      topology: "${tpu_config.topology}"
                                    networkConfig:
                                      network: "${tpu_config.vpc_network}"
                                      subnetwork: "${tpu_config.vpc_subnet}"
                                      enableExternalIps: ${tpu_config.enable_external_ips}
                                    serviceAccount:
                                      email: "${pipeline_sa}"
                                      scope: ["https://www.googleapis.com/auth/cloud-platform"]
                                    metadata:
                                      startup-script: ${jsonencode(gencast_tpu_startup_script)}
                                      date: $${gencast_action.date}
                                      forecast-regions: $${json.encode_to_string(gencast_action.regions)}
                                      common-bucket: $${common_bucket}
                                      region-buckets: $${json.encode_to_string(region_buckets)}
                                      gencast-image: "${tpu_config.image}"
                                      artifact-registry-host: "${tpu_config.artifact_registry_host}"
                                      expected-global-devices: "${tpu_config.global_device_count}"
                                      expected-local-devices: "${tpu_config.local_device_count}"
                                      expected-process-count: "${tpu_config.process_count}"
                                      ensemble-members: "${tpu_config.global_device_count}"
                        result: gencast_create_operation
                      except:
                        as: e
                        steps:
                          - handle_gencast_submit_error:
                              switch:
                                - condition: $${default(map.get(e, "code"), 0) == 409}
                                  next: poll_gencast_completion
                                - condition: true
                                  raise: $${e}
                  # The TPU API's queuedResources.create returns a long-running
                  # operation. http.post finishes as soon as the operation is
                  # accepted; if the underlying create later errors (no capacity,
                  # quota, bad accelerator config), the QR is never made and the
                  # workflow would otherwise silently poll a phantom resource.
                  # Poll the operation until done and raise on error.
                  - prepare_gencast_submit_poll:
                      assign:
                        - gencast_submit_op_name: $${gencast_create_operation.body.name}
                        - gencast_submit_op_url: $${"https://tpu.googleapis.com/v2alpha1/" + gencast_submit_op_name}
                        - gencast_submit_op_polls: 0
                  - poll_gencast_submit_operation:
                      call: http.get
                      args:
                        url: $${gencast_submit_op_url}
                        auth:
                          type: OAuth2
                          scopes: ["https://www.googleapis.com/auth/cloud-platform"]
                      result: gencast_submit_op
                  - check_gencast_submit_operation_done:
                      switch:
                        - condition: $${default(map.get(gencast_submit_op.body, "done"), false) == true}
                          next: check_gencast_submit_operation_error
                        - condition: $${gencast_submit_op_polls >= 60}
                          next: raise_gencast_submit_op_timeout
                        - condition: true
                          next: sleep_gencast_submit_operation
                  - sleep_gencast_submit_operation:
                      call: sys.sleep
                      args:
                        seconds: 10
                  - bump_gencast_submit_operation_polls:
                      assign:
                        - gencast_submit_op_polls: $${gencast_submit_op_polls + 1}
                      next: poll_gencast_submit_operation
                  - check_gencast_submit_operation_error:
                      switch:
                        - condition: $${default(map.get(gencast_submit_op.body, "error"), null) != null}
                          next: raise_gencast_submit_op_error
                        - condition: true
                          next: poll_gencast_completion
                  - raise_gencast_submit_op_error:
                      raise: '$${"GenCast queued resource create operation failed: " + json.encode_to_string(gencast_submit_op.body.error)}'
                  - raise_gencast_submit_op_timeout:
                      raise: '$${"GenCast queued resource create operation did not finish in 10 minutes: " + gencast_submit_op_name}'
                  - poll_gencast_completion:
                      call: pipeline_state
                      args:
                        base_url: $${pipeline_state_url}
                        date: $${requested_date}
                      result: gencast_poll_state
                  - check_gencast_outputs:
                      assign:
                        - gencast_remaining_action: $${default(map.get(gencast_poll_state.actions.models_to_run_by_model, "gencast"), default_model_action)}
                  - maybe_cleanup_gencast_success:
                      switch:
                        - condition: $${gencast_remaining_action.date == ""}
                          next: cleanup_gencast_success
                        - condition: true
                          next: get_gencast_qr_state
                  - get_gencast_qr_state:
                      try:
                        call: http.get
                        args:
                          url: $${gencast_qr_url}
                          auth:
                            type: OAuth2
                            scopes: ["https://www.googleapis.com/auth/cloud-platform"]
                        result: gencast_qr
                      except:
                        as: e
                        steps:
                          - handle_gencast_qr_get_error:
                              switch:
                                - condition: $${default(map.get(e, "code"), 0) == 404}
                                  next: raise_gencast_qr_missing
                                - condition: true
                                  raise: $${e}
                  - check_gencast_qr_state:
                      switch:
                        - condition: $${gencast_qr.body.state.state == "FAILED" or gencast_qr.body.state.state == "SUSPENDED"}
                          next: cleanup_gencast_failed
                        - condition: $${gencast_poll_count >= ${tpu_config.max_polls}}
                          next: cleanup_gencast_timeout
                        - condition: true
                          next: sleep_gencast
                  - sleep_gencast:
                      call: sys.sleep
                      args:
                        seconds: ${tpu_config.poll_interval_seconds}
                  - increment_gencast_poll:
                      assign:
                        - gencast_poll_count: $${gencast_poll_count + 1}
                      next: poll_gencast_completion
                  - cleanup_gencast_success:
                      call: delete_gencast_queued_resource
                      args:
                        queued_resource_url: $${gencast_qr_url}
                  - gencast_done:
                      assign:
                        - gencast_branch_done: true
                  - cleanup_gencast_failed:
                      call: delete_gencast_queued_resource
                      args:
                        queued_resource_url: $${gencast_qr_url}
                      next: raise_gencast_failed
                  - raise_gencast_failed:
                      raise: '$${"GenCast TPU queued resource failed before expected outputs were present: " + gencast_qr_url + " state=" + gencast_qr.body.state.state}'
                  - cleanup_gencast_timeout:
                      call: delete_gencast_queued_resource
                      args:
                        queued_resource_url: $${gencast_qr_url}
                      next: raise_gencast_timeout
                  - raise_gencast_timeout:
                      raise: '$${"GenCast TPU output did not appear before workflow timeout: " + gencast_qr_url}'
                  - raise_gencast_qr_missing:
                      raise: '$${"GenCast TPU queued resource not found (404) — never created or deleted before outputs appeared: " + gencast_qr_url}'
%{ endif ~}

    - probe_state_post_models:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state_post_models

    - log_post_model_plan:
        call: sys.log
        args:
          severity: INFO
          text: $${"post-model actions=" + json.encode_to_string(state_post_models.actions)}

    # -------------------------------------------------------------------------
    # 3. Blend any region/date whose configured model outputs are now present.
    # -------------------------------------------------------------------------
    - blend_loop:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_blend_action:
                assign:
                  - blend_action: $${default(map.get(state_post_models.actions.regions_to_blend_by_region, region_name), default_region_action)}
            - maybe_blend:
                switch:
                  - condition: $${blend_action.date == ""}
                    next: end_blend_iteration
                  - condition: true
                    next: verify_before_blend
            - verify_before_blend:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.postprocess.name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: DATE
                              value: $${blend_action.date}
                            - name: FORECAST_REGION
                              value: $${region_name}
                            - name: GCS_COMMON_BUCKET
                              value: $${common_bucket}
            - run_blend:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.blend.name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: DATE
                              value: $${blend_action.date}
                            - name: FORECAST_REGION
                              value: $${region_name}
                            - name: GCS_COMMON_BUCKET
                              value: $${common_bucket}
                            - name: GCS_REGION_BUCKETS
                              value: $${json.encode_to_string(region_buckets)}
            - end_blend_iteration:
                assign:
                  - last_blend_region_checked: $${region_name}

    - probe_state_post_blend:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state_post_blend

    # -------------------------------------------------------------------------
    # 4. Sync final regional outputs whose expected files are present.
    # -------------------------------------------------------------------------
    - sync_loop:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_sync_inputs:
                assign:
                  - region_cfg: $${map.get(regions, region_name)}
                  - sync_action: $${default(map.get(state_post_blend.actions.regions_to_sync_by_region, region_name), default_region_action)}
            - maybe_sync:
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
            - end_sync_iteration:
                assign:
                  - last_sync_region_checked: $${region_name}

    - maybe_return_partial:
        switch:
          - condition: $${len(state_post_blend.actions.blocked) > 0}
            next: return_partial
          - condition: true
            next: return_completed

    - return_completed:
        return:
          status: "completed"
          state: $${state_post_blend}

    - return_partial:
        return:
          status: "partial"
          blocked: $${state_post_blend.actions.blocked}
          state: $${state_post_blend}

    - return_checked:
        return:
          status: "checked"
          state: $${state}


# -----------------------------------------------------------------------------
# Subroutines
# -----------------------------------------------------------------------------

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
            audience: $${base_url}
        result: response
    - return_body:
        return: $${response.body}

delete_gencast_queued_resource:
  params: [queued_resource_url]
  steps:
    - delete_resource:
        try:
          call: http.delete
          args:
            url: $${queued_resource_url}
            auth:
              type: OAuth2
              scopes: ["https://www.googleapis.com/auth/cloud-platform"]
        except:
          as: e
          steps:
            - ignore_missing_resource:
                switch:
                  - condition: $${default(map.get(e, "code"), 0) == 404}
                    next: deleted
                  - condition: true
                    raise: $${e}
    - deleted:
        return: true
