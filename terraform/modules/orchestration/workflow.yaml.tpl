# -----------------------------------------------------------------------------
# Monsoon Pipeline Workflow
# Drives every branching decision off a single pipeline-state HTTP response:
# external IC discovery, GCS-cached IC presence, per-model forecast completeness,
# blendable date, and sync-needed date are all returned in one call.
# -----------------------------------------------------------------------------

main:
  params: [args]
  steps:
    - init:
        assign:
          - region: $${args.region}
          - requested_date: $${default(map.get(args, "date"), "")}
          - action: $${default(map.get(args, "action"), "run")}
          - project_id: "${project_id}"
          - common_bucket: "${common_bucket}"
          - region_buckets: ${jsonencode(region_buckets)}
          - region_bucket: $${default(map.get(region_buckets, region), common_bucket)}
          - pipeline_state_url: "${pipeline_state_url}"

    - call_pipeline_state_initial:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          region: $${region}
          date: $${requested_date}
        result: state

    - extract_initial_state:
        assign:
          - aifs_ic_date: $${state.models.aifs.ic_date}
          - aifs_ic_in_gcs: $${state.models.aifs.ic_in_gcs}
          - aifs_complete: $${state.models.aifs.forecast_complete}
          - aifs_ens_complete: $${state.models.aifs_ens.forecast_complete}
          - neuralgcm_ic_date: $${state.models.neuralgcm.ic_date}
          - neuralgcm_ic_in_gcs: $${state.models.neuralgcm.ic_in_gcs}
          - neuralgcm_complete: $${state.models.neuralgcm.forecast_complete}
          - blend_needs_run: $${state.blend.needs_run}
          - blend_date: $${state.blend.date}
          - sync_needs_run: $${state.sync.needs_run}
          - sync_date: $${state.sync.date}

    - log_initial_plan:
        call: sys.log
        args:
          severity: INFO
          text: $${"Pipeline-state initial region=" + region
                  + " aifs_ic=" + aifs_ic_date + " aifs_complete=" + string(aifs_complete)
                  + " aifs_ens_complete=" + string(aifs_ens_complete)
                  + " neuralgcm_ic=" + neuralgcm_ic_date + " neuralgcm_complete=" + string(neuralgcm_complete)
                  + " blend_needs_run=" + string(blend_needs_run) + " blend_date=" + blend_date
                  + " sync_needs_run=" + string(sync_needs_run) + " sync_date=" + sync_date}

    - maybe_return_checked:
        switch:
          - condition: $${action == "check"}
            next: return_checked
          - condition: true
            next: maybe_run_models

    - maybe_run_models:
        switch:
          - condition: $${(aifs_ic_date == "" or (aifs_complete and aifs_ens_complete)) and (neuralgcm_ic_date == "" or neuralgcm_complete)}
            next: post_models_state
          - condition: true
            next: run_models

    - run_models:
        parallel:
          branches:
            - run_aifs:
                steps:
                  - maybe_skip_aifs:
                      switch:
                        - condition: $${aifs_ic_date == "" or (aifs_complete and aifs_ens_complete)}
                          next: aifs_done
                        - condition: true
                          next: maybe_download_aifs_ic
                  - maybe_download_aifs_ic:
                      switch:
                        - condition: $${aifs_ic_in_gcs}
                          next: write_aifs_date_marker
                        - condition: true
                          next: download_aifs_ic
                  - download_aifs_ic:
                      call: googleapis.run.v2.projects.locations.jobs.run
                      args:
                        name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.downloader.name}"
                        body:
                          overrides:
                            containerOverrides:
                              - env:
                                  - name: SOURCE
                                    value: ecmwf
                                  - name: DATE
                                    value: $${aifs_ic_date}
                                  - name: FORECAST_REGION
                                    value: $${region}
                      result: aifs_download_result
                  - write_aifs_date_marker:
                      call: write_text_object
                      args:
                        bucket: $${common_bucket}
                        path: "intermediate/latest_ecmwf_date.txt"
                        content: $${aifs_ic_date}
                  - run_aifs_batch_jobs:
                      parallel:
                        branches:
                          - run_aifs_deterministic:
                              steps:
                                - maybe_skip_aifs_deterministic:
                                    switch:
                                      - condition: $${aifs_complete}
                                        next: aifs_deterministic_done
                                      - condition: true
                                        next: create_aifs_job
                                - create_aifs_job:
                                    call: googleapis.batch.v1.projects.locations.jobs.create
                                    args:
                                      parent: $${"projects/" + project_id + "/locations/${region}"}
                                      jobId: $${"aifs-" + region + "-" + text.replace_all(aifs_ic_date, "T", "-") + "-" + string(int(sys.now()))}
                                      body:
                                        taskGroups:
                                          - taskCount: 1
                                            taskSpec:
                                              computeResource:
                                                cpuMilli: 8000
                                                memoryMib: 32768
                                              maxRetryCount: 1
                                              maxRunDuration: "1800s"
                                              runnables:
                                                - container:
                                                    imageUri: "${batch_config.image}"
                                                    enableImageStreaming: ${batch_config.image_streaming}
                                                  environment:
                                                    variables:
                                                      DATE: $${aifs_ic_date}
                                                      AIFS_MODEL: AIFS
                                                      FORECAST_REGION: $${region}
                                                      GCS_BUCKET: $${region_bucket}
                                                      GCS_COMMON_BUCKET: $${common_bucket}
                                                      GCS_WEIGHTS_BUCKET: "${weights_bucket}"
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
                                    result: aifs_job
                                - poll_aifs:
                                    steps:
                                      - get_aifs_status:
                                          call: googleapis.batch.v1.projects.locations.jobs.get
                                          args:
                                            name: $${aifs_job.name}
                                          result: aifs_status
                                      - check_aifs_state:
                                          switch:
                                            - condition: $${aifs_status.status.state == "SUCCEEDED"}
                                              next: aifs_deterministic_done
                                            - condition: $${aifs_status.status.state == "FAILED"}
                                              raise: '$${"AIFS job failed " + aifs_job.name}'
                                      - sleep_aifs:
                                          call: sys.sleep
                                          args:
                                            seconds: 60
                                          next: get_aifs_status
                                - aifs_deterministic_done:
                                    assign:
                                      - aifs_deterministic_branch_done: true

                          - run_aifs_ensemble:
                              steps:
                                - maybe_skip_aifs_ensemble:
                                    switch:
                                      - condition: $${aifs_ens_complete}
                                        next: aifs_ensemble_done
                                      - condition: true
                                        next: create_aifs_ens_job
                                - create_aifs_ens_job:
                                    call: googleapis.batch.v1.projects.locations.jobs.create
                                    args:
                                      parent: $${"projects/" + project_id + "/locations/${region}"}
                                      jobId: $${"aifs-ens-" + region + "-" + text.replace_all(aifs_ic_date, "T", "-") + "-" + string(int(sys.now()))}
                                      body:
                                        taskGroups:
                                          - taskCount: 1
                                            taskSpec:
                                              computeResource:
                                                cpuMilli: 8000
                                                memoryMib: 65536
                                              maxRetryCount: 1
                                              maxRunDuration: "7200s"
                                              runnables:
                                                - container:
                                                    imageUri: "${batch_config.image}"
                                                    enableImageStreaming: ${batch_config.image_streaming}
                                                  environment:
                                                    variables:
                                                      DATE: $${aifs_ic_date}
                                                      AIFS_MODEL: AIFS_ENS
                                                      FORECAST_REGION: $${region}
                                                      GCS_BUCKET: $${region_bucket}
                                                      GCS_COMMON_BUCKET: $${common_bucket}
                                                      GCS_WEIGHTS_BUCKET: "${weights_bucket}"
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
                                    result: aifs_ens_job
                                - poll_aifs_ens:
                                    steps:
                                      - get_aifs_ens_status:
                                          call: googleapis.batch.v1.projects.locations.jobs.get
                                          args:
                                            name: $${aifs_ens_job.name}
                                          result: aifs_ens_status
                                      - check_aifs_ens_state:
                                          switch:
                                            - condition: $${aifs_ens_status.status.state == "SUCCEEDED"}
                                              next: aifs_ensemble_done
                                            - condition: $${aifs_ens_status.status.state == "FAILED"}
                                              raise: '$${"AIFS-ENS job failed " + aifs_ens_job.name}'
                                      - sleep_aifs_ens:
                                          call: sys.sleep
                                          args:
                                            seconds: 60
                                          next: get_aifs_ens_status
                                - aifs_ensemble_done:
                                    assign:
                                      - aifs_ensemble_branch_done: true
                  - aifs_done:
                      assign:
                        - aifs_branch_done: true

            - run_neuralgcm:
                steps:
                  - maybe_skip_neuralgcm:
                      switch:
                        - condition: $${neuralgcm_ic_date == "" or neuralgcm_complete}
                          next: neuralgcm_done
                        - condition: true
                          next: maybe_download_neuralgcm_ic
                  - maybe_download_neuralgcm_ic:
                      switch:
                        - condition: $${neuralgcm_ic_in_gcs}
                          next: create_neuralgcm_job
                        - condition: true
                          next: download_neuralgcm_ic
                  - download_neuralgcm_ic:
                      call: googleapis.run.v2.projects.locations.jobs.run
                      args:
                        name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.downloader.name}"
                        body:
                          overrides:
                            containerOverrides:
                              - env:
                                  - name: SOURCE
                                    value: ncep
                                  - name: DATE
                                    value: $${neuralgcm_ic_date}
                                  - name: FORECAST_REGION
                                    value: $${region}
                      result: neuralgcm_download_result
                  - create_neuralgcm_job:
                      call: googleapis.batch.v1.projects.locations.jobs.create
                      args:
                        parent: $${"projects/" + project_id + "/locations/${region}"}
                        jobId: $${"neuralgcm-" + region + "-" + text.replace_all(neuralgcm_ic_date, "T", "-") + "-" + string(int(sys.now()))}
                        body:
                          taskGroups:
                            - taskCount: 1
                              taskSpec:
                                computeResource:
                                  cpuMilli: 8000
                                  memoryMib: 65536
                                maxRetryCount: 1
                                maxRunDuration: "3600s"
                                runnables:
                                  - container:
                                      imageUri: "${batch_config.neuralgcm_image}"
                                      enableImageStreaming: ${batch_config.image_streaming}
                                    environment:
                                      variables:
                                        DATE: $${neuralgcm_ic_date}
                                        FORECAST_REGION: $${region}
                                        GCS_BUCKET: $${region_bucket}
                                        GCS_COMMON_BUCKET: $${common_bucket}
                                        GCS_WEIGHTS_BUCKET: "${weights_bucket}"
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
                      result: neuralgcm_job
                  - poll_neuralgcm:
                      steps:
                        - get_neuralgcm_status:
                            call: googleapis.batch.v1.projects.locations.jobs.get
                            args:
                              name: $${neuralgcm_job.name}
                            result: neuralgcm_status
                        - check_neuralgcm_state:
                            switch:
                              - condition: $${neuralgcm_status.status.state == "SUCCEEDED"}
                                next: neuralgcm_done
                              - condition: $${neuralgcm_status.status.state == "FAILED"}
                                raise: '$${"NeuralGCM job failed " + neuralgcm_job.name}'
                        - sleep_neuralgcm:
                            call: sys.sleep
                            args:
                              seconds: 120
                            next: get_neuralgcm_status
                  - neuralgcm_done:
                      assign:
                        - neuralgcm_branch_done: true

    - post_models_state:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          region: $${region}
          date: $${requested_date}
        result: state_after_models

    - extract_post_models_state:
        assign:
          - blend_needs_run: $${state_after_models.blend.needs_run}
          - blend_date: $${state_after_models.blend.date}

    - maybe_blend:
        switch:
          - condition: $${blend_needs_run}
            next: write_blend_aifs_marker
          - condition: true
            next: post_blend_state

    - write_blend_aifs_marker:
        call: write_text_object
        args:
          bucket: $${common_bucket}
          path: "intermediate/latest_ecmwf_date.txt"
          content: $${blend_date}

    - postprocess:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.postprocess.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: DATE
                      value: $${blend_date}
                    - name: FORECAST_REGION
                      value: $${region}
                    - name: GCS_BUCKET
                      value: $${region_bucket}
                    - name: GCS_COMMON_BUCKET
                      value: $${common_bucket}
        result: postprocess_result

    - blend:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.blend.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: DATE
                      value: $${blend_date}
                    - name: FORECAST_REGION
                      value: $${region}
                    - name: GCS_BUCKET
                      value: $${region_bucket}
                    - name: GCS_COMMON_BUCKET
                      value: $${common_bucket}
        result: blend_result

    - post_blend_state:
        call: pipeline_state
        args:
          base_url: $${pipeline_state_url}
          region: $${region}
          date: $${requested_date}
        result: state_after_blend

    - extract_post_blend_state:
        assign:
          - sync_needs_run: $${state_after_blend.sync.needs_run}
          - sync_date: $${state_after_blend.sync.date}

    - maybe_sync:
        switch:
          - condition: $${sync_needs_run}
            next: sync
          - condition: true
            next: return_result

    - sync:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.sync.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: DATE
                      value: $${sync_date}
                    - name: FORECAST_REGION
                      value: $${region}
                    - name: GCS_BUCKET
                      value: $${region_bucket}
                    - name: GCS_COMMON_BUCKET
                      value: $${common_bucket}
        result: sync_result

    - return_result:
        return:
          status: "completed"
          region: $${region}
          aifs_ic_date: $${aifs_ic_date}
          neuralgcm_ic_date: $${neuralgcm_ic_date}
          blend_date: $${blend_date}
          sync_date: $${sync_date}
          aifs_ran: $${aifs_ic_date != "" and not aifs_complete}
          aifs_ens_ran: $${aifs_ic_date != "" and not aifs_ens_complete}
          neuralgcm_ran: $${neuralgcm_ic_date != "" and not neuralgcm_complete}
          blend_ran: $${blend_needs_run}
          sync_ran: $${sync_needs_run}

    - return_checked:
        return:
          status: "checked"
          region: $${region}
          state: $${state}

# -----------------------------------------------------------------------------
# Subroutines
# -----------------------------------------------------------------------------

pipeline_state:
  params: [base_url, region, date]
  steps:
    - build_url:
        switch:
          - condition: $${date == ""}
            next: set_url_no_date
          - condition: true
            next: set_url_with_date
    - set_url_no_date:
        assign:
          - url: $${base_url + "/state?region=" + region}
        next: fetch
    - set_url_with_date:
        assign:
          - url: $${base_url + "/state?region=" + region + "&date=" + date}
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

write_text_object:
  params: [bucket, path, content]
  steps:
    - write_object:
        call: http.post
        args:
          url: $${"https://storage.googleapis.com/upload/storage/v1/b/" + bucket + "/o?uploadType=media&name=" + text.replace_all(path, "/", "%2F")}
          auth:
            type: OAuth2
          headers:
            Content-Type: text/plain
          body: $${content}
        result: write_result
    - return_result:
        return: $${write_result}
