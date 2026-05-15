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
                                text: $${"ecmwf download failed for " + ecmwf_download.date + ": " + json.encode_to_string(e)}
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
                                text: $${"ncep download failed for " + ncep_download.date + ": " + json.encode_to_string(e)}
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
    # -------------------------------------------------------------------------
    - run_models:
        parallel:
          branches:
%{ for model in models_in_use ~}
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
                                      sizeGb: ${batch_config.boot_disk_gb}
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
                                - condition: $${e.code == 409}
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
