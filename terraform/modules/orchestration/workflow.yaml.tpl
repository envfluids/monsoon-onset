# -----------------------------------------------------------------------------
# Monsoon Pipeline Workflow
# Greedily starts model work as soon as each model's 00z IC is available.
# Final products still require AIFS and NeuralGCM outputs for the same date.
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
          - gcs_bucket: "monsoon-${environment}-data-${project_id}"
          - ic_checker_url: "${ic_checker_url}"
          - last_processed_date: ""

    - get_source_dates:
        switch:
          - condition: $${requested_date == ""}
            next: call_ic_checker
          - condition: true
            next: set_manual_source_dates

    - call_ic_checker:
        call: http.get
        args:
          url: $${ic_checker_url + "/check?lookback_days=7"}
          auth:
            type: OIDC
            audience: $${ic_checker_url}
        result: ic_check

    - set_checked_source_dates:
        assign:
          - ecmwf_date: $${default(map.get(ic_check.body, "ecmwf_date"), "")}
          - ncep_date: $${default(map.get(ic_check.body, "ncep_date"), "")}
        next: set_final_candidate_empty

    - set_manual_source_dates:
        assign:
          - ecmwf_date: $${requested_date}
          - ncep_date: $${requested_date}

    - set_final_candidate_empty:
        assign:
          - final_candidate_date: ""

    - choose_final_candidate:
        switch:
          - condition: $${ecmwf_date == "" or ncep_date == ""}
            next: maybe_return_checked
          - condition: $${int(text.replace_all(ecmwf_date, "T", "")) <= int(text.replace_all(ncep_date, "T", ""))}
            next: set_final_candidate_ecmwf
          - condition: true
            next: set_final_candidate_ncep

    - set_final_candidate_ecmwf:
        assign:
          - final_candidate_date: $${ecmwf_date}
        next: maybe_return_checked

    - set_final_candidate_ncep:
        assign:
          - final_candidate_date: $${ncep_date}

    - maybe_return_checked:
        switch:
          - condition: $${action == "check"}
            next: return_checked
          - condition: true
            next: maybe_read_last_processed

    - maybe_read_last_processed:
        switch:
          - condition: $${final_candidate_date == ""}
            next: check_aifs_final_output
          - condition: true
            next: check_latest_marker

    - check_latest_marker:
        call: object_exists
        args:
          bucket: $${gcs_bucket}
          path: $${region + "/latest.txt"}
        result: latest_marker_exists

    - maybe_download_latest_marker:
        switch:
          - condition: $${latest_marker_exists}
            next: read_last_processed
          - condition: true
            next: check_aifs_final_output

    - read_last_processed:
        call: http.get
        args:
          url: $${"https://storage.googleapis.com/download/storage/v1/b/" + gcs_bucket + "/o/" + region + "%2Flatest.txt?alt=media"}
          auth:
            type: OAuth2
        result: last_processed_file

    - set_last_processed:
        assign:
          - last_processed_date: $${last_processed_file.body}

    - check_aifs_final_output:
        call: model_outputs_exist
        args:
          bucket: $${gcs_bucket}
          region: $${region}
          model: "aifs"
          date: $${final_candidate_date}
        result: aifs_final_output_exists

    - check_neuralgcm_final_output:
        call: model_outputs_exist
        args:
          bucket: $${gcs_bucket}
          region: $${region}
          model: "neuralgcm"
          date: $${final_candidate_date}
        result: neuralgcm_final_output_exists

    - check_aifs_latest_output:
        call: model_outputs_exist
        args:
          bucket: $${gcs_bucket}
          region: $${region}
          model: "aifs"
          date: $${ecmwf_date}
        result: aifs_latest_output_exists

    - check_neuralgcm_latest_output:
        call: model_outputs_exist
        args:
          bucket: $${gcs_bucket}
          region: $${region}
          model: "neuralgcm"
          date: $${ncep_date}
        result: neuralgcm_latest_output_exists

    - select_aifs_target:
        switch:
          - condition: $${final_candidate_date != "" and not aifs_final_output_exists}
            next: set_aifs_target_final
          - condition: $${ecmwf_date != "" and not aifs_latest_output_exists}
            next: set_aifs_target_latest
          - condition: true
            next: set_aifs_target_empty

    - set_aifs_target_final:
        assign:
          - aifs_target_date: $${final_candidate_date}
        next: select_neuralgcm_target

    - set_aifs_target_latest:
        assign:
          - aifs_target_date: $${ecmwf_date}
        next: select_neuralgcm_target

    - set_aifs_target_empty:
        assign:
          - aifs_target_date: ""

    - select_neuralgcm_target:
        switch:
          - condition: $${final_candidate_date != "" and not neuralgcm_final_output_exists}
            next: set_neuralgcm_target_final
          - condition: $${ncep_date != "" and not neuralgcm_latest_output_exists}
            next: set_neuralgcm_target_latest
          - condition: true
            next: set_neuralgcm_target_empty

    - set_neuralgcm_target_final:
        assign:
          - neuralgcm_target_date: $${final_candidate_date}
        next: log_greedy_plan

    - set_neuralgcm_target_latest:
        assign:
          - neuralgcm_target_date: $${ncep_date}
        next: log_greedy_plan

    - set_neuralgcm_target_empty:
        assign:
          - neuralgcm_target_date: ""

    - log_greedy_plan:
        call: sys.log
        args:
          text: $${"Greedy plan region=" + region + " ecmwf_date=" + ecmwf_date + " ncep_date=" + ncep_date + " final_candidate=" + final_candidate_date + " aifs_target=" + aifs_target_date + " neuralgcm_target=" + neuralgcm_target_date}
          severity: INFO

    - maybe_return_no_work:
        switch:
          - condition: $${aifs_target_date == "" and neuralgcm_target_date == "" and (final_candidate_date == "" or last_processed_date == final_candidate_date)}
            next: return_no_data
          - condition: true
            next: run_models

    - run_models:
        parallel:
          branches:
            - run_aifs:
                steps:
                  - maybe_skip_aifs:
                      switch:
                        - condition: $${aifs_target_date == ""}
                          next: aifs_done
                        - condition: true
                          next: check_aifs_ic
                  - check_aifs_ic:
                      call: raw_ic_exists
                      args:
                        bucket: $${gcs_bucket}
                        region: $${region}
                        source: "ecmwf"
                        date: $${aifs_target_date}
                      result: aifs_ic_exists
                  - maybe_download_aifs_ic:
                      switch:
                        - condition: $${aifs_ic_exists}
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
                                    value: $${aifs_target_date}
                                  - name: FORECAST_REGION
                                    value: $${region}
                      result: aifs_download_result
                  - write_aifs_date_marker:
                      call: write_text_object
                      args:
                        bucket: $${gcs_bucket}
                        path: $${region + "/intermediate/latest_ecmwf_date.txt"}
                        content: $${aifs_target_date}
                  - create_aifs_job:
                      call: googleapis.batch.v1.projects.locations.jobs.create
                      args:
                        parent: $${"projects/" + project_id + "/locations/${region}"}
                        jobId: $${"aifs-" + region + "-" + text.replace_all(aifs_target_date, "T", "-")}
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
                                        DATE: $${aifs_target_date}
                                        FORECAST_REGION: $${region}
                                        GCS_BUCKET: $${gcs_bucket}
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
                                next: aifs_done
                              - condition: $${aifs_status.status.state == "FAILED"}
                                raise: '$${"AIFS job failed " + aifs_job.name}'
                        - sleep_aifs:
                            call: sys.sleep
                            args:
                              seconds: 60
                            next: get_aifs_status
                  - aifs_done:
                      assign:
                        - aifs_branch_done: true

            - run_neuralgcm:
                steps:
                  - maybe_skip_neuralgcm:
                      switch:
                        - condition: $${neuralgcm_target_date == ""}
                          next: neuralgcm_done
                        - condition: true
                          next: check_neuralgcm_ic
                  - check_neuralgcm_ic:
                      call: raw_ic_exists
                      args:
                        bucket: $${gcs_bucket}
                        region: $${region}
                        source: "ncep"
                        date: $${neuralgcm_target_date}
                      result: neuralgcm_ic_exists
                  - maybe_download_neuralgcm_ic:
                      switch:
                        - condition: $${neuralgcm_ic_exists}
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
                                    value: $${neuralgcm_target_date}
                                  - name: FORECAST_REGION
                                    value: $${region}
                      result: neuralgcm_download_result
                  - create_neuralgcm_job:
                      call: googleapis.batch.v1.projects.locations.jobs.create
                      args:
                        parent: $${"projects/" + project_id + "/locations/${region}"}
                        jobId: $${"neuralgcm-" + region + "-" + text.replace_all(neuralgcm_target_date, "T", "-")}
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
                                        DATE: $${neuralgcm_target_date}
                                        FORECAST_REGION: $${region}
                                        GCS_BUCKET: $${gcs_bucket}
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

    - check_final_aifs_outputs:
        call: model_outputs_exist
        args:
          bucket: $${gcs_bucket}
          region: $${region}
          model: "aifs"
          date: $${final_candidate_date}
        result: final_aifs_outputs_exist

    - check_final_neuralgcm_outputs:
        call: model_outputs_exist
        args:
          bucket: $${gcs_bucket}
          region: $${region}
          model: "neuralgcm"
          date: $${final_candidate_date}
        result: final_neuralgcm_outputs_exist

    - maybe_run_final_stages:
        switch:
          - condition: $${final_candidate_date == "" or last_processed_date == final_candidate_date or not final_aifs_outputs_exist or not final_neuralgcm_outputs_exist}
            next: return_partial
          - condition: true
            next: write_final_aifs_marker

    - write_final_aifs_marker:
        call: write_text_object
        args:
          bucket: $${gcs_bucket}
          path: $${region + "/intermediate/latest_ecmwf_date.txt"}
          content: $${final_candidate_date}

    - postprocess:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.postprocess.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: DATE
                      value: $${final_candidate_date}
                    - name: FORECAST_REGION
                      value: $${region}
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
                      value: $${final_candidate_date}
                    - name: FORECAST_REGION
                      value: $${region}
        result: blend_result

    - sync:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.sync.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: DATE
                      value: $${final_candidate_date}
                    - name: FORECAST_REGION
                      value: $${region}
        result: sync_result

    - return_result:
        return:
          status: "completed"
          region: $${region}
          date: $${final_candidate_date}
          ecmwf_date: $${ecmwf_date}
          ncep_date: $${ncep_date}

    - return_partial:
        return:
          status: "partial"
          region: $${region}
          final_candidate_date: $${final_candidate_date}
          ecmwf_date: $${ecmwf_date}
          ncep_date: $${ncep_date}
          aifs_target_date: $${aifs_target_date}
          neuralgcm_target_date: $${neuralgcm_target_date}
          final_aifs_outputs_exist: $${final_aifs_outputs_exist}
          final_neuralgcm_outputs_exist: $${final_neuralgcm_outputs_exist}

    - return_checked:
        return:
          status: "checked"
          region: $${region}
          final_candidate_date: $${final_candidate_date}
          ecmwf_date: $${ecmwf_date}
          ncep_date: $${ncep_date}

    - return_no_data:
        return:
          status: "no_data"
          region: $${region}
          final_candidate_date: $${final_candidate_date}
          ecmwf_date: $${ecmwf_date}
          ncep_date: $${ncep_date}

object_exists:
  params: [bucket, path]
  steps:
    - list_object:
        call: http.get
        args:
          url: $${"https://storage.googleapis.com/storage/v1/b/" + bucket + "/o?prefix=" + text.replace_all(path, "/", "%2F") + "&maxResults=1"}
          auth:
            type: OAuth2
        result: object_list
    - set_items:
        assign:
          - object_items: $${default(map.get(object_list.body, "items"), [])}
    - check_item_count:
        switch:
          - condition: $${len(object_items) == 0}
            next: return_false
    - return_result:
        return: $${object_items[0].name == path}
    - return_false:
        return: false

raw_ic_exists:
  params: [bucket, region, source, date]
  steps:
    - empty_date:
        switch:
          - condition: $${date == ""}
            next: return_false
    - choose_path:
        switch:
          - condition: $${source == "ecmwf"}
            next: set_ecmwf_path
          - condition: true
            next: set_ncep_path
    - set_ecmwf_path:
        assign:
          - path: $${region + "/raw/ecmwf/" + date + "/input_state_" + date + ".pkl"}
        next: check_path
    - set_ncep_path:
        assign:
          - path: $${region + "/raw/ncep/" + date + "/gdas_" + date + ".pgrb2"}
    - check_path:
        call: object_exists
        args:
          bucket: $${bucket}
          path: $${path}
        result: exists
    - return_exists:
        return: $${exists}
    - return_false:
        return: false

model_outputs_exist:
  params: [bucket, region, model, date]
  steps:
    - empty_date:
        switch:
          - condition: $${date == ""}
            next: return_false
    - check_tp:
        call: object_exists
        args:
          bucket: $${bucket}
          path: $${region + "/output/" + model + "/" + date + "/tp_" + date + ".nc"}
        result: tp_exists
    - check_sji:
        call: object_exists
        args:
          bucket: $${bucket}
          path: $${region + "/output/" + model + "/" + date + "/sji_" + date + ".nc"}
        result: sji_exists
    - return_result:
        return: $${tp_exists and sji_exists}
    - return_false:
        return: false

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
