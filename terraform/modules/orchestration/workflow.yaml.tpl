# -----------------------------------------------------------------------------
# Monsoon Pipeline Workflow
# Orchestrates the full forecast pipeline
# -----------------------------------------------------------------------------

main:
  params: [args]
  steps:
    - init:
        assign:
          - region: $${args.region}
          - date: $${default(map.get(args, "date"), "")}
          - action: $${default(map.get(args, "action"), "run")}
          - project_id: "${project_id}"
          - gcs_bucket: "monsoon-${environment}-data-${project_id}"

    # -----------------------------------------------------------------------------
    # For "check" action: run the downloader to detect latest available data date.
    # The downloader writes the date to GCS and exits 0 if new data is available,
    # or exits 0 with an empty file if no new data (workflow exits early).
    # For "run" action with no date: same flow, but we proceed regardless.
    # -----------------------------------------------------------------------------
    - get_date:
        switch:
          - condition: $${date == ""}
            next: run_date_check
          - condition: true
            next: log_start

    - run_date_check:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.downloader.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: ACTION
                      value: get_latest_date
                    - name: SOURCE
                      value: ecmwf
                    - name: FORECAST_REGION
                      value: $${region}
        result: date_check_execution

    - read_date_from_gcs:
        try:
          call: http.get
          args:
            url: $${"https://storage.googleapis.com/download/storage/v1/b/" + gcs_bucket + "/o/" + region + "%2Fintermediate%2Flatest_date.txt?alt=media"}
            auth:
              type: OAuth2
          result: date_file
        except:
          as: e
          steps:
            - no_date_available:
                call: sys.log
                args:
                  text: $${"No new data available for region=" + region + ", exiting."}
                  severity: INFO
                next: return_no_data

    - set_date_from_gcs:
        assign:
          - date: $${date_file.body}

    - check_empty_date:
        switch:
          - condition: $${date == ""}
            next: return_no_data

    - read_last_processed:
        try:
          call: http.get
          args:
            url: $${"https://storage.googleapis.com/download/storage/v1/b/" + gcs_bucket + "/o/" + region + "%2Flatest.txt?alt=media"}
            auth:
              type: OAuth2
          result: last_processed_file
        except:
          as: e
          steps:
            - no_prior_run:
                next: check_action

    - check_already_processed:
        switch:
          - condition: $${last_processed_file.body == date}
            next: return_no_data

    # For "check"-only action, stop after confirming data is available
    - check_action:
        switch:
          - condition: $${action == "check"}
            next: return_no_data  # Date is logged above; exit without running full pipeline

    - log_start:
        call: sys.log
        args:
          text: $${"Starting pipeline for region=" + region + " date=" + date}
          severity: INFO

    # -----------------------------------------------------------------------------
    # Download data in parallel
    # Cloud Run Jobs are invoked via the Run v2 API; googleapis.run.v2.projects.locations.jobs.run
    # blocks until the execution completes.
    # -----------------------------------------------------------------------------
    - download_data:
        parallel:
          branches:
            - download_ecmwf:
                steps:
                  - run_ecmwf_download:
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
                                    value: $${date}
                                  - name: FORECAST_REGION
                                    value: $${region}
                      result: ecmwf_result
            - download_ncep:
                steps:
                  - run_ncep_download:
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
                                    value: $${date}
                                  - name: FORECAST_REGION
                                    value: $${region}
                      result: ncep_result

    # -----------------------------------------------------------------------------
    # Run models in parallel
    # Both AIFS (GPU) and NeuralGCM (GPU) run on Cloud Batch with Docker containers.
    # -----------------------------------------------------------------------------
    - run_models:
        parallel:
          branches:
            - run_aifs:
                steps:
                  - create_aifs_job:
                      call: googleapis.batch.v1.projects.locations.jobs.create
                      args:
                        parent: $${"projects/" + project_id + "/locations/${region}"}
                        jobId: $${"aifs-" + region + "-" + text.replace_all(date, "T", "-")}
                        body:
                          taskGroups:
                            - taskCount: 1
                              taskSpec:
                                computeResource:
                                  cpuMilli: 8000
                                  memoryMib: 32768
                                maxRetryCount: 1
                                maxRunDuration: "3600s"
                                runnables:
                                  - container:
                                      imageUri: "${batch_config.image}"
                                    environment:
                                      variables:
                                        DATE: $${date}
                                        FORECAST_REGION: $${region}
                                        GCS_BUCKET: $${gcs_bucket}
                                        GCS_WEIGHTS_BUCKET: "${weights_bucket}"
                          allocationPolicy:
                            instances:
                              - policy:
                                  machineType: "${batch_config.machine_type}"
                                  provisioningModel: "${batch_config.preemptible ? "SPOT" : "STANDARD"}"
                                  accelerators:
                                    - type: "${batch_config.gpu_type}"
                                      count: 1
                            location:
                              allowedLocations: ["regions/${region}"]
                            network:
                              networkInterfaces:
                                - network: "${batch_config.vpc_network}"
                                  subnetwork: "${batch_config.vpc_subnet}"
                                  noExternalIpAddress: true
                          logsPolicy:
                            destination: CLOUD_LOGGING
                      result: aifs_job

                  # Poll until the Batch job reaches a terminal state
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
                        - aifs_complete: true

            - run_neuralgcm:
                steps:
                  - create_neuralgcm_job:
                      call: googleapis.batch.v1.projects.locations.jobs.create
                      args:
                        parent: $${"projects/" + project_id + "/locations/${region}"}
                        jobId: $${"neuralgcm-" + region + "-" + text.replace_all(date, "T", "-")}
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
                                      imageUri: "${batch_config.neuralgcm_image}"
                                    environment:
                                      variables:
                                        DATE: $${date}
                                        FORECAST_REGION: $${region}
                                        GCS_BUCKET: $${gcs_bucket}
                                        GCS_WEIGHTS_BUCKET: "${weights_bucket}"
                          allocationPolicy:
                            instances:
                              - policy:
                                  machineType: "${batch_config.machine_type}"
                                  provisioningModel: "${batch_config.preemptible ? "SPOT" : "STANDARD"}"
                                  accelerators:
                                    - type: "${batch_config.gpu_type}"
                                      count: 1
                            location:
                              allowedLocations: ["regions/${region}"]
                            network:
                              networkInterfaces:
                                - network: "${batch_config.vpc_network}"
                                  subnetwork: "${batch_config.vpc_subnet}"
                                  noExternalIpAddress: true
                          logsPolicy:
                            destination: CLOUD_LOGGING
                      result: neuralgcm_job

                  # Poll until the Batch job reaches a terminal state
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
                        - neuralgcm_complete: true

    # -----------------------------------------------------------------------------
    # Post-process, blend, and sync — sequential
    # -----------------------------------------------------------------------------
    - postprocess:
        call: googleapis.run.v2.projects.locations.jobs.run
        args:
          name: "projects/${project_id}/locations/${region}/jobs/${cloud_run_jobs.postprocess.name}"
          body:
            overrides:
              containerOverrides:
                - env:
                    - name: DATE
                      value: $${date}
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
                      value: $${date}
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
                      value: $${date}
                    - name: FORECAST_REGION
                      value: $${region}
        result: sync_result

    - log_complete:
        call: sys.log
        args:
          text: $${"Pipeline completed for region=" + region + " date=" + date}
          severity: INFO

    - return_result:
        return:
          status: "completed"
          region: $${region}
          date: $${date}

    - return_no_data:
        return:
          status: "no_data"
          region: $${region}
