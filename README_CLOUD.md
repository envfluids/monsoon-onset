# Cloud Pipeline Architecture

End-to-end reference for the GCP-based monsoon onset forecast pipeline. Covers every Google Cloud service involved, how they connect, and the exact sequence of execution.

---

## Google Cloud Services Used

| Service | Role |
|---------|------|
| Cloud Scheduler | Cron trigger - fires the workflow on a schedule |
| Cloud Workflows | Orchestrator - sequences and conditionally branches all pipeline stages |
| Cloud Run Jobs | Lightweight compute for downloader, postprocess, blend, sync |
| Cloud Batch | GPU compute for AIFS and NeuralGCM inference |
| Cloud Storage (GCS) | Data lake - raw inputs, intermediate state, model outputs, weights |
| Artifact Registry | Container image registry for all pipeline images |
| Cloud Logging | Centralized logs from all containers and the workflow itself |
| Serverless VPC Access | Connector allowing Cloud Run Jobs to reach private VPC resources |
| Cloud NAT | Outbound internet access for containers with no public IP |
| Cloud Compute (VPC) | Private network, subnet, firewall rules |
| IAM Service Accounts | Identity for workflow orchestration and pipeline jobs |

---

## Infrastructure Layout

Provisioned by OpenTofu in `terraform/`. Two environments share the same module structure:

```
terraform/
  environments/
    dev/main.tf       <- dev-specific variables (preemptible GPUs, 6h schedule, LOG_ALL_CALLS)
    prod/main.tf      <- production configuration
  modules/
    networking/       <- VPC, subnet, Cloud NAT, firewall rules
    storage/          <- GCS buckets, Artifact Registry, pipeline service account
    compute/          <- Cloud Run Jobs, IC checker service, Batch job template
    orchestration/    <- Cloud Workflows, Cloud Scheduler, Pub/Sub topics, workflow SA
    monitoring/       <- Alerting (disabled in dev)
```

### Buckets

Two GCS buckets are created per environment:

**`monsoon-{env}-data-{project_id}`** (main data bucket)

The pipeline's working storage. Organized by region:

```
{region}/
  raw/
    ecmwf/{date}/input_state_{date}.pkl       <- ECMWF IFS initial conditions
    ncep/{date}/gdas_{date}.pgrb2             <- NCEP GDAS initial conditions
  intermediate/
    latest_date.txt                           <- latest ECMWF date from opendata API
    latest_ecmwf_date.txt                     <- actual ECMWF date written by downloader
    neuralgcm_{date}_done                     <- NeuralGCM completion marker
  output/
    aifs/{date}/tp/tp_{date}.nc               <- AIFS precipitation
    aifs/{date}/sji/sji_{date}.nc             <- AIFS Somali Jet Index
    aifs/{date}/tcw/tcw_{date}.nc             <- AIFS total column water vapor
    neuralgcm/{date}/tp/tp_{date}.nc          <- NeuralGCM precipitation (30-member merged)
    neuralgcm/{date}/sji/sji_{date}.nc        <- NeuralGCM SJI
    neuralgcm/{date}/tcw/tcw_{date}.nc        <- NeuralGCM total column water vapor
    blend/{date}/blend_output_summary.csv     <- final probabilistic onset forecast
    blend/{date}/maps/                        <- forecast maps
  latest.txt                                  <- date of last successfully completed run
```

`raw/` and `intermediate/` objects expire after 30 days (dev) per lifecycle rules. `output/` is retained.

**`monsoon-{env}-weights-{project_id}`** (weights bucket)

Read-only static files. Never auto-deleted, always versioned:

```
aifs/
  aifs-single-mse-1.1.ckpt                   <- AIFS model checkpoint
  EKR/mir_16_linear/{hash}.npz               <- sparse IFS->N320 transform matrix
  grids/grid_2p0.txt                          <- 2-degree CDO remap grid
  data/india_mask_2p0.nc                      <- land mask
neuralgcm/
  models_v1_precip_stochastic_precip_2_8_deg.pkl  <- NeuralGCM checkpoint
  forcings/SST-SeaIce_clim_1979_2017_no_leap.nc   <- SST/sea ice climatology
blend/
  support/large/                              <- multinomial regression coefficient CSVs
```

### Container Images

All images are stored in a single **Artifact Registry** Docker repository (`monsoon-{env}-containers`) in the same region. The pipeline service account has `roles/artifactregistry.reader` on this repository. Images are referenced by tag (`latest`) - Terraform ignores image changes in the Cloud Run Job definitions so containers can be updated independently via `docker push` without a `tofu apply`.

---

## Service Accounts and IAM

Two service accounts are created:

**`monsoon-{env}-workflow`** (orchestration module)
The identity used by Cloud Workflows and Cloud Scheduler to invoke jobs and write logs.

| Role | Scope | Purpose |
|------|-------|---------|
| `roles/run.developer` | Project | Invoke and run Cloud Run Jobs with overrides |
| `roles/batch.jobsEditor` | Project | Submit and poll Cloud Batch model jobs |
| `roles/workflows.invoker` | Project | Allow Cloud Scheduler to trigger workflow executions |
| `roles/logging.logWriter` | Project | Write `sys.log` entries from the workflow |
| `roles/storage.objectAdmin` | Main data bucket | Read and write lightweight workflow marker objects |
| `roles/iam.serviceAccountUser` | Pipeline SA | Attach the pipeline SA to Cloud Run and Batch jobs |

**`monsoon-{env}-pipeline`** (storage module)
The identity used by all Cloud Run Job containers at runtime.

| Role | Scope | Purpose |
|------|-------|---------|
| `roles/storage.objectAdmin` | Main data bucket | Read and write all pipeline data |
| `roles/storage.objectViewer` | Weights bucket | Download model checkpoints and static files |
| `roles/artifactregistry.reader` | Container repository | Pull images |

## Networking

A dedicated VPC (`monsoon-{env}-vpc`) is created with:

- **Subnet** with `private_ip_google_access = true`, enabling containers to reach GCS, Artifact Registry, and other Google APIs over the internal network without leaving Google's backbone.
- **Cloud NAT** on a Cloud Router for outbound internet access (needed by the downloader to reach ECMWF opendata and NCEP servers). No container has a public IP.
- **Serverless VPC Access Connector** (`monsoon-{env}-connector`) bridging Cloud Run's serverless environment into the VPC subnet. All Cloud Run Jobs route `ALL_TRAFFIC` through this connector. In dev it uses `e2-micro` instances (2-3 instances); in prod `e2-standard-4` (2-10 instances).
- **Firewall rules**: allow internal TCP/UDP within the subnet CIDR; allow SSH from IAP range `35.235.240.0/20`; allow Google health check ranges.

---

## Workflow Execution - Step by Step

The workflow is defined in `terraform/modules/orchestration/workflow.yaml.tpl` and deployed as a **Cloud Workflows** resource (`monsoon-{env}-pipeline`). It is a single YAML document with a `main` entrypoint that takes an `args` map.

### 1. Cloud Scheduler fires

**Service: Cloud Scheduler** (`monsoon-{env}-trigger-india`)

Runs on a cron schedule (`0 */6 * * *` in dev, more frequent in prod). Sends an HTTP POST to the Cloud Workflows Executions API:

```
POST https://workflowexecutions.googleapis.com/v1/projects/.../workflows/monsoon-{env}-pipeline/executions
```

Body:
```json
{
  "argument": "{\"region\": \"india\"}"
}
```

`date` and `action` are intentionally omitted - the workflow detects the latest date itself. Authentication uses the workflow SA's OAuth2 token with scope `cloud-platform`.

### 2. init

**Service: Cloud Workflows (assign step)**

Initializes variables from the `args` map:

- `region` - read directly from `args.region` (always present)
- `date` - `default(map.get(args, "date"), "")` - empty string if not provided by scheduler
- `action` - `default(map.get(args, "action"), "run")` - defaults to `"run"`
- `project_id` - hardcoded from Terraform template substitution at deploy time
- `gcs_bucket` - `monsoon-{env}-data-{project_id}`, also substituted at deploy time

`map.get` safely handles missing keys (returns null); `default` converts null to the fallback. This avoids the `KeyError: key not found` that `args.date` would throw for absent keys.

### 3. get_date

**Service: Cloud Workflows (switch step)**

If `date == ""` (scheduler-triggered path): go to `run_date_check`.
If `date` was explicitly provided (manual invocation): skip directly to `log_start`.

### 4. run_date_check

**Service: Cloud Run Jobs** (via `googleapis.run.v2.projects.locations.jobs.run`)

Invokes the **downloader** Cloud Run Job (`monsoon-{env}-downloader`) with environment variable overrides:

```
ACTION=get_latest_date
SOURCE=ecmwf
FORECAST_REGION={region}
```

The container (`docker/downloader/src/main.py`) calls `ecmwf.opendata.Client().latest()` to ping the ECMWF opendata API and get the most recent available forecast date. It writes the result to:

```
gs://monsoon-{env}-data-{project_id}/{region}/intermediate/latest_date.txt
```

The Cloud Workflows call blocks until the Cloud Run Job execution completes (Cloud Run Jobs are synchronous from the Workflows perspective - the API call returns only when the job finishes).

### 5. read_date_from_gcs

**Service: Cloud Workflows (http.get step)**

The workflow reads `latest_date.txt` directly via the Cloud Storage JSON API:

```
GET https://storage.googleapis.com/download/storage/v1/b/{bucket}/o/{region}%2Fintermediate%2Flatest_date.txt?alt=media
```

Authentication uses the workflow SA's OAuth2 token. The workflow SA has `roles/storage.objectViewer` on the main bucket for exactly this purpose.

If the file doesn't exist (the download returned no data), the `except` block logs "No new data available" and exits via `return_no_data`.

### 6. set_date_from_gcs

**Service: Cloud Workflows (assign step)**

Sets `date = date_file.body` - the string content of `latest_date.txt`, e.g. `20260421T00`.

### 7. check_empty_date

**Service: Cloud Workflows (switch step)**

If the downloader wrote an empty string to `latest_date.txt` (meaning no ECMWF data was available), go to `return_no_data`.

### 8. read_last_processed

**Service: Cloud Workflows (http.get step)**

Reads `{region}/latest.txt` from GCS - the file written by the sync container at the end of every successful pipeline run:

```
GET https://storage.googleapis.com/download/storage/v1/b/{bucket}/o/{region}%2Flatest.txt?alt=media
```

If the file does not exist (first ever run for this region), the `except` block jumps directly to `check_action`, skipping the duplicate check.

### 9. check_already_processed

**Service: Cloud Workflows (switch step)**

Compares `last_processed_file.body == date`. If they match, the pipeline has already been run successfully for this date and the workflow exits via `return_no_data`. This prevents re-running the full pipeline on every scheduler tick when ECMWF data hasn't changed.

### 10. check_action

**Service: Cloud Workflows (switch step)**

If `action == "check"` (manual invocation for dry-run): exit without running the pipeline. Under normal scheduler-triggered operation `action == "run"`, so execution continues.

### 11. log_start

**Service: Cloud Logging** (via `sys.log`)

Writes `Starting pipeline for region={region} date={date}` at INFO severity to Cloud Logging. Visible in the Logs Explorer under the workflow execution resource.

### 12. download_data (parallel)

**Service: Cloud Run Jobs** - two branches run concurrently via a `parallel` block.

**Branch A - download_ecmwf:**
Invokes the downloader job with:
```
SOURCE=ecmwf
DATE={date}
FORECAST_REGION={region}
```
The container fetches the latest ECMWF IFS analysis (0.25-degree global) via `download_ic.get_data()`, saves it as `input_state_{ecmwf_date}.pkl`, and uploads to:
```
gs://{bucket}/{region}/raw/ecmwf/{ecmwf_date}/input_state_{ecmwf_date}.pkl
```
It also writes the actual ECMWF date (which may differ slightly from `DATE`) to:
```
gs://{bucket}/{region}/intermediate/latest_ecmwf_date.txt
```

**Branch B - download_ncep:**
Invokes the downloader job with:
```
SOURCE=ncep
DATE={date}
FORECAST_REGION={region}
```
The container fetches the latest NCEP GDAS GRIB2 file via `download_ncep.get_data()` and uploads to:
```
gs://{bucket}/{region}/raw/ncep/{date}/gdas_{date}.pgrb2
```

Both branches block until their respective Cloud Run Job executions complete. Cloud Workflows parallelism means both downloads run simultaneously - the workflow does not proceed until both finish.

### 13. run_models (parallel)

**Service: Cloud Batch** - two branches run concurrently, each creating a GPU Batch job.

Cloud Batch is used instead of Cloud Run for AIFS and NeuralGCM because they require GPU accelerators and multi-hour runtimes beyond Cloud Run's limits.

**Branch A - run_aifs:**

Creates a Cloud Batch job with ID `aifs-{region}-{date}` (e.g. `aifs-india-20260421T00`). The job ID is unique per date - if the pipeline somehow runs twice for the same date, the second `create` call will fail with a conflict error.

Batch job spec:
- 1 task, 1 retry, max 3600s (1 hour)
- 8 vCPU, 32 GiB RAM
- GPU accelerator: type and count from `batch_config` (e.g. `nvidia-tesla-a100`)
- Machine type from `batch_config` (e.g. `a2-highgpu-1g`)
- SPOT provisioning in dev, STANDARD in prod
- Runs the AIFS container image from Artifact Registry
- Private networking only (`noExternalIpAddress: true`) within the VPC subnet
- Logs to Cloud Logging via `logsPolicy.destination: CLOUD_LOGGING`

The AIFS container (`docker/aifs/src/main.py`) does:
1. Downloads `input_state_{ecmwf_date}.pkl` from GCS (reads `latest_ecmwf_date.txt` to find the right filename)
2. Downloads the AIFS checkpoint (`aifs-single-mse-1.1.ckpt`) from the weights bucket
3. Downloads the sparse IFS-to-N320 transform matrix (`.npz`) from the weights bucket
4. Runs `run_model.py` (deterministic 41-day, 6-hourly forecast)
5. Runs `post_process.py` (regrid to 2-degree, compute SJI, TP, TCW)
6. Uploads outputs to `gs://{bucket}/{region}/output/aifs/{ecmwf_date}/`

After creating the job, the workflow polls `googleapis.batch.v1.projects.locations.jobs.get` every 60 seconds until `status.state == "SUCCEEDED"` or `"FAILED"`.

**Branch B - run_neuralgcm:**

Creates a Cloud Batch job with ID `neuralgcm-{region}-{date}`. Spec:
- 1 task, 1 retry, max 7200s (2 hours)
- 8 vCPU, 64 GiB RAM (larger than AIFS due to 30-member ensemble)
- Same GPU and network configuration as AIFS

The NeuralGCM container (`docker/neuralgcm/src/main.py`) does:
1. Downloads `gdas_{date}.pgrb2` from GCS
2. Downloads the NeuralGCM checkpoint (`.pkl`) and SST/sea ice forcing file from the weights bucket
3. Runs `preprocess.py` - NCL interpolation of GRIB2 to NetCDF in the format NeuralGCM expects
4. Runs `run_model.py` - 30-member stochastic ensemble, 45-day forecast using JAX on GPU
5. Runs `post_process.py` - per-member SJI, TP, TCW computation
6. Runs `post_process_merge.py` - merges all 30 members into ensemble statistics
7. Uploads outputs to `gs://{bucket}/{region}/output/neuralgcm/{date}/`
8. Writes a completion marker: `gs://{bucket}/{region}/intermediate/neuralgcm_{date}_done`

The workflow polls every 120 seconds (longer than AIFS because NeuralGCM takes more time).

### 14. postprocess

**Service: Cloud Run Jobs** (`monsoon-{env}-postprocess`)

16 GiB RAM, 4 vCPU, 30-minute timeout.

This is a verification gate, not a processing step. The actual post-processing (SJI, regridding, TP/TCW computation) happens inside the AIFS and NeuralGCM containers. This step checks that all required output files exist in GCS before allowing the blend to proceed:

```
{region}/output/aifs/{ecmwf_date}/tp_0p25/tp_{ecmwf_date}.nc
{region}/output/aifs/{ecmwf_date}/tp/tp_{ecmwf_date}.nc
{region}/output/aifs/{ecmwf_date}/sji/sji_{ecmwf_date}.nc
{region}/output/aifs/{ecmwf_date}/tcw/tcw_{ecmwf_date}.nc
{region}/output/neuralgcm/{date}/tp/tp_{date}.nc
{region}/output/neuralgcm/{date}/sji/sji_{date}.nc
{region}/output/neuralgcm/{date}/tcw/tcw_{date}.nc
```

The ECMWF date is read from `latest_ecmwf_date.txt`. If any file is missing, the container raises a `RuntimeError` and the Cloud Run Job fails, which fails the workflow.

### 15. blend

**Service: Cloud Run Jobs** (`monsoon-{env}-blend`)

8 GiB RAM, 4 vCPU, 30-minute timeout.

The blend container (`docker/blend/src/main.py`) does:
1. Reads `latest_ecmwf_date.txt` to resolve the AIFS date
2. Downloads AIFS `tp/tp_{aifs_date}.nc` and NeuralGCM `tp/tp_{date}.nc` from GCS
3. Downloads climatology CSVs from the weights bucket (`blend/support/large/`)
4. Runs `blend/utils/main.py --date {date} --source google` - this is the original science script unmodified:
   - Reads multinomial logistic regression coefficients from `blend/data/support/multinom_coefs_full.csv`
   - Computes onset probability for each 2-degree grid cell across bins: week1, week2, week3, week4, later
   - Generates `blend_output_summary.csv` and forecast maps
5. Uploads all outputs to `gs://{bucket}/{region}/output/blend/{date}/`

### 16. sync

**Service: Cloud Run Jobs** (`monsoon-{env}-sync`)

1 GiB RAM, 1 vCPU, 10-minute timeout.

The sync container (`docker/sync/src/main.py`) does:
1. Downloads blend outputs from `gs://{bucket}/{region}/output/blend/{date}/`
2. Optionally syncs to Google Drive (controlled by `ENABLE_DRIVE` env var)
3. Writes the current date to `gs://{bucket}/{region}/latest.txt`

Writing `latest.txt` is the critical final step - it is the dedup signal that `read_last_processed` checks at the top of the next scheduler invocation. If the pipeline fails at any earlier stage, `latest.txt` is not updated, so the next scheduler tick will retry the full pipeline for the same date.

### 17. log_complete / return_result

**Service: Cloud Logging + Cloud Workflows**

Logs `Pipeline completed for region={region} date={date}` and returns:
```json
{"status": "completed", "region": "india", "date": "20260421T00"}
```

---

## Execution Flow Diagram

```
Cloud Scheduler (cron)
        |
        | POST /executions  {region: "india"}
        v
Cloud Workflows: monsoon-{env}-pipeline
        |
        +-- init (assign vars)
        |
        +-- get_date: date=="" ?
        |       |
        |       +-- run_date_check
        |       |     Cloud Run Job: downloader
        |       |     ACTION=get_latest_date, SOURCE=ecmwf
        |       |     writes: intermediate/latest_date.txt
        |       |
        |       +-- read_date_from_gcs (http.get -> GCS)
        |       +-- set_date_from_gcs
        |       +-- check_empty_date --------> return_no_data
        |       +-- read_last_processed (http.get -> GCS)
        |       +-- check_already_processed -> return_no_data
        |       +-- check_action ("check") --> return_no_data
        |
        +-- log_start
        |
        +-- download_data [parallel]
        |       |
        |       +-- Cloud Run Job: downloader (SOURCE=ecmwf)
        |       |     fetches IFS -> uploads raw/ecmwf/{date}/
        |       |     writes: intermediate/latest_ecmwf_date.txt
        |       |
        |       +-- Cloud Run Job: downloader (SOURCE=ncep)
        |             fetches GDAS -> uploads raw/ncep/{date}/
        |
        +-- run_models [parallel]
        |       |
        |       +-- Cloud Batch Job: aifs-india-{date}
        |       |     GPU inference -> uploads output/aifs/{date}/
        |       |     polls every 60s
        |       |
        |       +-- Cloud Batch Job: neuralgcm-india-{date}
        |             GPU inference -> uploads output/neuralgcm/{date}/
        |             polls every 120s
        |
        +-- postprocess
        |     Cloud Run Job: postprocess
        |     verifies all 4 required NC files exist in GCS
        |
        +-- blend
        |     Cloud Run Job: blend
        |     downloads AIFS+NeuralGCM TP, runs regression, uploads blend/{date}/
        |
        +-- sync
        |     Cloud Run Job: sync
        |     writes latest.txt -> marks run complete for dedup
        |
        +-- log_complete -> return_result
```

---

## Dev vs Prod Differences

| Setting | Dev | Prod |
|---------|-----|------|
| Schedule | Every 6 hours | Configurable (default every 15 min) |
| Workflow call log level | `LOG_ALL_CALLS` (full execution history in console) | `LOG_NONE` |
| GPU provisioning | SPOT (preemptible) | STANDARD |
| Cloud Run Job deletion protection | Off | On |
| GCS retention | 30 days (raw/intermediate) | Configurable with NEARLINE archival |
| GCS versioning | Disabled | Enabled |
| Monitoring alerts | Disabled | Enabled |
| Force-destroy main bucket | Yes | No |

---

## Logs

| What | Where |
|------|-------|
| Workflow step execution, variable values | Cloud Logging: resource type `workflows.googleapis.com/Workflow` (dev only - requires `LOG_ALL_CALLS`) |
| Cloud Run Job container stdout/stderr | Cloud Logging: resource type `run.googleapis.com/CloudRunJob` |
| Cloud Batch job container stdout/stderr | Cloud Logging: resource type `batch.googleapis.com/Job` |
| Workflow execution history | Cloud Workflows console -> Executions tab |
