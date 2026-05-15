# -----------------------------------------------------------------------------
# Monsoon Pipeline Workflow — Region-Agnostic, Multi-Region
#
# 1. Probe pipeline-state for the global multi-region view.
# 2. Download external ICs (ECMWF, NCEP) in parallel — guarded by per-source
#    presence flags (skipped when not needed by any configured region).
# 3. Run each model once across all its regions (shared IC + full-field; the
#    container itself loops over regions internally).
# 4. Per-region downstream: postprocess gate → blend (if region uses it)
#    → sync (if region uses it).
#
# Region names appear only in `for` iterators and template-time renderings of
# var.regions — the workflow body itself is region-agnostic.
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
          # Default fallbacks for pipeline-state response fields that may be
          # omitted when a source/model/region isn't configured. The expression
          # language doesn't accept inline map literals, so we pre-define them.
          - default_ic:
              present: true
              date: ""
          - default_model_info:
              date: ""
              complete: true
          - default_region_block: {}
          - default_blend_info:
              date: ""
              present: false
          - default_sync_info:
              date: ""
              needs_run: false

    - probe_state_initial:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state

    - extract_initial:
        assign:
          - ecmwf_state: $${default(map.get(state.ic, "ecmwf"), default_ic)}
          - ncep_state: $${default(map.get(state.ic, "ncep"), default_ic)}

    - log_initial_plan:
        call: sys.log
        args:
          severity: INFO
          text: $${"PS date=" + state.date + " ecmwf_date=" + ecmwf_state.date + " ecmwf_present=" + string(ecmwf_state.present) + " ncep_date=" + ncep_state.date + " ncep_present=" + string(ncep_state.present)}

    - maybe_return_checked:
        switch:
          - condition: $${action == "check"}
            next: return_checked
          - condition: true
            next: maybe_download_ics

    # -------------------------------------------------------------------------
    # IC download (parallel; skipped per-branch when source not needed or
    # already present in GCS)
    # -------------------------------------------------------------------------
    - maybe_download_ics:
        parallel:
          branches:
            - download_ecmwf:
                steps:
                  - check_ecmwf:
                      switch:
                        - condition: $${ecmwf_state.present or not(ecmwf_state.date)}
                          next: ecmwf_done
                        - condition: true
                          next: run_download_ecmwf
                  - run_download_ecmwf:
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
                                    value: $${ecmwf_state.date}
                  - ecmwf_done:
                      assign:
                        - ecmwf_branch_done: true
            - download_ncep:
                steps:
                  - check_ncep:
                      switch:
                        - condition: $${ncep_state.present or not(ncep_state.date)}
                          next: ncep_done
                        - condition: true
                          next: run_download_ncep
                  - run_download_ncep:
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
                                    value: $${ncep_state.date}
                  - ncep_done:
                      assign:
                        - ncep_branch_done: true

    # -------------------------------------------------------------------------
    # Re-probe state so post-IC view drives model launch decisions
    # -------------------------------------------------------------------------
    - probe_state_post_ic:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state_post_ic

    - extract_post_ic:
        assign:
          - models_state: $${state_post_ic.models}

    # -------------------------------------------------------------------------
    # Run models (parallel; one branch per model in use; per-branch skip when
    # model already complete across all its regions)
    # -------------------------------------------------------------------------
    - maybe_run_models:
        parallel:
          branches:
%{ for model in models_in_use ~}
            - run_${model}:
                steps:
                  - load_${model}_info:
                      assign:
                        - ${model}_info: $${default(map.get(models_state, "${model}"), default_model_info)}
                  - maybe_skip_${model}:
                      switch:
                        - condition: $${not(${model}_info.date) or ${model}_info.complete}
                          next: ${model}_done
                        - condition: true
                          next: submit_${model}_batch
                  - submit_${model}_batch:
                      call: googleapis.batch.v1.projects.locations.jobs.create
                      args:
                        parent: $${"projects/" + project_id + "/locations/${region}"}
                        jobId: $${"${model}-" + text.replace_all(${model}_info.date, "T", "-") + "-" + string(int(sys.now()))}
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
                                        DATE: $${${model}_info.date}
                                        MODEL: "${model}"
                                        FORECAST_REGIONS: ${jsonencode(jsonencode(regions_by_model[model]))}
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
                      result: ${model}_job
                  - poll_${model}:
                      steps:
                        - get_${model}_status:
                            call: googleapis.batch.v1.projects.locations.jobs.get
                            args:
                              name: $${${model}_job.name}
                            result: ${model}_status
                        - check_${model}_state:
                            switch:
                              - condition: $${${model}_status.status.state == "SUCCEEDED"}
                                next: ${model}_done
                              - condition: $${${model}_status.status.state == "FAILED"}
                                raise: '$${"${model} job failed " + ${model}_job.name}'
                        - sleep_${model}:
                            call: sys.sleep
                            args:
                              seconds: ${model == "neuralgcm" ? 120 : 60}
                            next: get_${model}_status
                  - ${model}_done:
                      assign:
                        - ${model}_branch_done: true
%{ endfor ~}

    # -------------------------------------------------------------------------
    # Re-probe state for per-region downstream decisions
    # -------------------------------------------------------------------------
    - probe_state_post_models:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          date: $${requested_date}
        result: state_post_models

    - extract_post_models:
        assign:
          - per_region: $${state_post_models.per_region}
          - primary_date: $${state_post_models.date}

    # -------------------------------------------------------------------------
    # Per-region downstream stages (sequential; cheap enough that we don't
    # need to parallelize across regions)
    # -------------------------------------------------------------------------
    - per_region_loop:
        for:
          value: region_name
          in: $${region_names}
          steps:
            - load_region_block:
                assign:
                  - region_cfg: $${map.get(regions, region_name)}
                  - region_block: $${default(map.get(per_region, region_name), default_region_block)}
                  - blend_info: $${default(map.get(region_block, "blend"), default_blend_info)}
                  - sync_info: $${default(map.get(region_block, "sync"), default_sync_info)}
            - maybe_postprocess:
                switch:
                  - condition: $${"blend" in region_cfg.stages or "sync" in region_cfg.stages}
                    next: postprocess
                  - condition: true
                    next: end_region_iteration
            - postprocess:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.postprocess.name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: DATE
                              value: $${primary_date}
                            - name: FORECAST_REGION
                              value: $${region_name}
                            - name: GCS_COMMON_BUCKET
                              value: $${common_bucket}
                result: postprocess_result
            - maybe_blend:
                switch:
                  - condition: $${"blend" in region_cfg.stages and blend_info.date != "" and not(blend_info.present)}
                    next: run_blend
                  - condition: true
                    next: maybe_sync
            - run_blend:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.blend.name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: DATE
                              value: $${blend_info.date}
                            - name: FORECAST_REGION
                              value: $${region_name}
                            - name: GCS_COMMON_BUCKET
                              value: $${common_bucket}
                            - name: GCS_REGION_BUCKETS
                              value: $${json.encode_to_string(region_buckets)}
                result: blend_result
            - maybe_sync:
                switch:
                  - condition: $${"sync" in region_cfg.stages and default(map.get(sync_info, "needs_run"), false)}
                    next: run_sync
                  - condition: true
                    next: end_region_iteration
            - run_sync:
                call: googleapis.run.v2.projects.locations.jobs.run
                args:
                  name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.sync.name}"
                  body:
                    overrides:
                      containerOverrides:
                        - env:
                            - name: DATE
                              value: $${sync_info.date}
                            - name: FORECAST_REGION
                              value: $${region_name}
                            - name: GCS_REGION_BUCKETS
                              value: $${json.encode_to_string(region_buckets)}
                            - name: SYNC_SPEC
                              value: $${json.encode_to_string(region_cfg.sync)}
                result: sync_result
            - end_region_iteration:
                assign:
                  - last_region_processed: $${region_name}

    - return_result:
        return:
          status: "completed"
          date: $${primary_date}
          regions: $${region_names}

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
