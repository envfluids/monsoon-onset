# Cloud Pipeline Architecture

End-to-end reference for the GCP-based monsoon onset forecast pipeline. Covers every Google Cloud service involved, how they connect, and the exact sequence of execution.

---

## Google Cloud Services Used

| Service | Role |
|---------|------|
| Cloud Scheduler | Cron trigger - fires the workflow on a schedule |
| Cloud Workflows | Orchestrator - sequences and conditionally branches all pipeline stages |
| Cloud Run Jobs | Lightweight compute for downloader, blend, sync |
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

One common data bucket, one bucket per forecast region, and one weights bucket are created per environment.

**`monsoon-{env}-common-data-{project_id}`** (common data bucket)

Shared working storage for initial conditions, intermediate markers, and full-field raw model forecasts. These paths are not region-prefixed:

```
raw/
  ecmwf/{date}/grib/*.grib2                 <- ECMWF IFS GRIB initial conditions
  gencast/sst/{date}/sst_{date}.nc          <- GenCast SST initial condition
  ncep/{date}/gdas_{date}.pgrb2             <- NCEP GDAS initial conditions
full_field/
  aifs/{date}/init_{date}.nc                <- AIFS full-field forecast
  aifs_ens/{date}/init_{date}.zarr/         <- AIFS-ENS full-field forecast
  neuralgcm/{date}/member_*.zarr/           <- NeuralGCM full-field member forecasts
  gencast/{date}/init_{date}.zarr/          <- GenCast full-field forecast
intermediate/
  latest_date.txt                           <- latest date marker written by downloader
  latest_ecmwf_date.txt                     <- actual ECMWF date written by downloader
  neuralgcm_{date}_done                     <- NeuralGCM completion marker
```

`raw/` and `intermediate/` objects expire after 30 days (dev) per lifecycle rules. `raw_forecast/` is retained and may transition to Nearline.

**`monsoon-{env}-{region}-data-{project_id}`** (regional output buckets)

Each forecast region gets its own output bucket. Only post-processed model products, blended outputs, and that region's final sync marker live here:

```
output/
  aifs/{date}/...                           <- AIFS post-processed products for this region
  aifs_ens/{date}/...                       <- AIFS-ENS post-processed products where configured
  neuralgcm/{date}/...                      <- NeuralGCM post-processed products where configured
  ncum/{date}/...                           <- NCUM products if provided for this region
  blend/{date}/blend_output_summary.csv     <- final probabilistic onset forecast
  blend/{date}/maps/                        <- forecast maps
latest.txt                                  <- date of last successfully completed run
```

`output/` is retained and may transition to Nearline.

**`monsoon-{env}-weights-{project_id}`** (weights bucket)

Read-only static files. Never auto-deleted, always versioned:

```
aifs/
  aifs-single-mse-1.1.ckpt                   <- AIFS model checkpoint
  aifs-ens-crps-1.0.ckpt                     <- AIFS-ENS model checkpoint
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
| `roles/tpu.admin` | Project | Submit, poll, and delete GenCast TPU queued resources |
| `roles/workflows.invoker` | Project | Allow Cloud Scheduler to trigger workflow executions |
| `roles/logging.logWriter` | Project | Write `sys.log` entries from the workflow |
| `roles/storage.objectAdmin` | Common and regional data buckets | Read and write lightweight workflow marker objects |
| `roles/iam.serviceAccountUser` | Pipeline SA | Attach the pipeline SA to Cloud Run, Batch, and TPU jobs |

**`monsoon-{env}-pipeline`** (storage module)
The identity used by all Cloud Run Job containers at runtime.

| Role | Scope | Purpose |
|------|-------|---------|
| `roles/storage.objectAdmin` | Common and regional data buckets | Read and write all pipeline data |
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
- `requested_date` - `default(map.get(args, "date"), "")` - empty string if not provided by scheduler
- `action` - `default(map.get(args, "action"), "run")` - defaults to `"run"`
- `project_id` - hardcoded from Terraform template substitution at deploy time
- `common_bucket` - `monsoon-{env}-common-data-{project_id}`, substituted at deploy time
- `region_bucket` - looked up from Terraform's `region_buckets` map for the requested forecast region

`map.get` safely handles missing keys (returns null); `default` converts null to the fallback. This avoids the `KeyError: key not found` that `args.date` would throw for absent keys.

### 3. call_pipeline_state_initial

**Service: Cloud Run Service** (`monsoon-{env}-pipeline-state`)

The workflow calls the pipeline-state service once at the start. If no date was supplied, the service probes external IC sources for the latest 00z ECMWF and NCEP cycles, then checks GCS for cached ICs, completed forecasts, blendable outputs, and pending sync work.

The state response drives the rest of the workflow:

- `models.aifs` and `models.aifs_ens`: ECMWF IC date, IC presence in the common bucket, and forecast completeness in the regional bucket
- `models.neuralgcm`: NCEP IC date, IC presence in the common bucket, and forecast completeness in the regional bucket
- `blend`: latest date where the inputs configured in `blend/utils/main.py` exist and blend output is missing
- `sync`: latest blended date that has not been marked complete in the regional bucket

### 4. check_action

**Service: Cloud Workflows (switch step)**

If `action == "check"` (manual invocation for dry-run): exit without running the pipeline. Under normal scheduler-triggered operation `action == "run"`, so execution continues.

### 5. maybe_run_models

**Service: Cloud Workflows (switch step)**

If all forecasts that can run are already complete, the workflow skips directly to the post-model state check. Otherwise it runs the model branches.

### 6. run_models (parallel)

**Service: Cloud Batch, Cloud TPU, and Cloud Run Jobs** - AIFS, NeuralGCM, and GenCast branches run concurrently after their ICs are present.

**Branch A - run_aifs:**

If ECMWF ICs are missing from the common bucket, the branch invokes the downloader job with:
```
SOURCE=ecmwf
DATE={aifs_ic_date}
FORECAST_REGION={region}
```

The container fetches ECMWF IFS GRIBs, downloads the matching GenCast SST IC, and uploads shared inputs to the common bucket:
```
gs://{common_bucket}/raw/ecmwf/{ecmwf_date}/grib/
gs://{common_bucket}/raw/gencast/sst/{ecmwf_date}/sst_{ecmwf_date}.nc
```

It also writes the actual ECMWF date to:
```
gs://{common_bucket}/intermediate/latest_ecmwf_date.txt
```

After ICs are present, the workflow creates separate Cloud Batch jobs for deterministic AIFS and, where a region requires it, AIFS-ENS. GenCast uses the same ECMWF IC source but runs on a TPU queued resource instead of the GPU Batch path.

**Branch B - run_neuralgcm:**

If NCEP ICs are missing from the common bucket, the branch invokes the downloader job with:
```
SOURCE=ncep
DATE={neuralgcm_ic_date}
FORECAST_REGION={region}
```

The container fetches the NCEP GDAS GRIB2 file and uploads it to:
```
gs://{common_bucket}/raw/ncep/{neuralgcm_ic_date}/gdas_{neuralgcm_ic_date}.pgrb2
```

Cloud Batch is used instead of Cloud Run for AIFS and NeuralGCM because they require GPU accelerators and multi-hour runtimes beyond Cloud Run's limits.

**AIFS Batch jobs:**

Creates separate Cloud Batch jobs with IDs `aifs-{region}-{date}-{timestamp}` and `aifs-ens-{region}-{date}-{timestamp}`. Each job uses the same AIFS container image but passes a different `AIFS_MODEL` value. Pipeline-state skips region/model pairs that do not produce post-processed outputs for the requested region.

Deterministic AIFS job spec:
- 1 task, 1 retry, max 1800s (30 minutes)
- 8 vCPU, 32 GiB RAM
- `AIFS_MODEL=AIFS`

AIFS-ENS job spec:
- 1 task, 1 retry, max 7200s (2 hours)
- 8 vCPU, 64 GiB RAM
- `AIFS_MODEL=AIFS_ENS`

Common job settings:
- GPU accelerator: type and count from `batch_config` (e.g. `nvidia-tesla-a100`)
- Machine type from `batch_config` (e.g. `a2-highgpu-1g`)
- SPOT provisioning in dev, STANDARD in prod
- Runs the AIFS container image from Artifact Registry
- Private networking only (`noExternalIpAddress: true`) within the VPC subnet
- Logs to Cloud Logging via `logsPolicy.destination: CLOUD_LOGGING`

The AIFS container (`docker/aifs/src/main.py`) does:
1. Downloads ECMWF GRIB files from the common bucket (reads `latest_ecmwf_date.txt` to find the right filename)
2. Downloads the requested AIFS checkpoint (`aifs-single-mse-1.1.ckpt` or `aifs-ens-crps-1.0.ckpt`) from the weights bucket
3. Downloads the sparse transform matrices (`.npz`) from the weights bucket
4. Runs `run_model.py` or `run_model_ENS.py`, depending on `AIFS_MODEL`
5. Runs `post_process.py --model AIFS|AIFS_ENS`
6. Uploads full-field forecasts to `raw_forecast/` in the common bucket and post-processed products to `output/` in the regional bucket

`AIFS_MODEL` defaults to `AIFS`; the cloud workflow sets it explicitly per Batch job.

After creating each job, the workflow polls `googleapis.batch.v1.projects.locations.jobs.get` every 60 seconds until `status.state == "SUCCEEDED"` or `"FAILED"`.

**Branch B - run_neuralgcm:**

Creates a Cloud Batch job with ID `neuralgcm-{region}-{date}`. Spec:
- 1 task, 1 retry, max 7200s (2 hours)
- 8 vCPU, 64 GiB RAM (larger than AIFS due to 30-member ensemble)
- Same GPU and network configuration as AIFS

The NeuralGCM container (`docker/neuralgcm/src/main.py`) does:
1. Downloads `gdas_{date}.pgrb2` from the common bucket
2. Downloads the NeuralGCM checkpoint (`.pkl`) and SST/sea ice forcing file from the weights bucket
3. Runs `preprocess.py` - NCL interpolation of GRIB2 to NetCDF in the format NeuralGCM expects
4. Runs `run_model.py` - 30-member stochastic ensemble, 45-day forecast using JAX on GPU
5. Runs `post_process.py` - per-member SJI, TP, TCW computation
6. Runs `post_process_merge.py` - merges all 30 members into ensemble statistics
7. Uploads full-field forecasts to `gs://{common_bucket}/raw_forecast/neuralgcm/{date}/`
8. Uploads post-processed products to `gs://{region_bucket}/output/neuralgcm/{date}/`
9. Writes a completion marker: `gs://{common_bucket}/intermediate/neuralgcm_{date}_done`

The workflow polls every 120 seconds (longer than AIFS because NeuralGCM takes more time).

**GenCast TPU queued resource:**

GenCast runs on a TPU v5p-64 slice with topology `2x4x4` (32 chips across 8 `ct5p-hightpu-4t` hosts). The default zone is `us-central1-a`; set `gencast_tpu_zone = "us-east5-a"` to move TPU capacity east. If the TPU zone is outside the primary region, Terraform creates a matching regional subnet and NAT on the existing VPC.

The workflow submits one queued resource per GenCast date, using a stable ID `gencast-{date}`. Duplicate submissions poll the existing queued resource. The startup script runs the GenCast container on every TPU VM host with JAX distributed initialization enabled. `run_gencast.py` logs and validates:
- global device count: `32`
- local device count per TPU VM: `4`
- process count: `8`

Only JAX process 0 publishes full-field and region outputs; the other TPU hosts participate in the distributed run and then exit without publishing duplicate artifacts. The TPU startup script mounts the common bucket at `/mnt/disks/common` with Cloud Storage FUSE and passes `GENCAST_ZARR_MIRROR_TARGET=/mnt/disks/common/full_field/gencast/{date}/init_{date}.zarr` into the container, so full-field Zarr components are mirrored asynchronously while inference is still generating chunks. The GenCast utility code does not enable this mirror unless the cloud wrapper or an operator sets the mirror environment variable, preserving the HPC local-output workflow.

### 7. blend

**Service: Cloud Run Jobs** (`monsoon-{env}-blend`)

8 GiB RAM, 4 vCPU, 30-minute timeout.

The actual model post-processing happens inside the model containers, and `pipeline-state` performs the blend-readiness gate by importing the lightweight configuration in `blend/utils/main.py`.

The blend container (`docker/blend/src/main.py`) does:
1. Selects the configured blends for the requested region from `blend/utils/main.py`
2. Downloads each configured input from GCS into the repo-shaped path expected by the science script
3. Downloads region blend support/coefficient files from `weights/blend/{region}/` into `/app/blend/data/`
4. Runs `blend/utils/main.py --date {date} --region {region}`:
   - Reads multinomial logistic regression coefficients from `blend/data/support/multinom_coefs_full.csv`
   - Computes onset probability for each 2-degree grid cell across bins: week1, week2, week3, week4, later
   - Generates `blend_output_summary.csv` and forecast maps
5. Uploads all outputs to `gs://{region_bucket}/output/blend/{date}/`

### 8. sync

**Service: Cloud Run Jobs** (`monsoon-{env}-sync`)

1 GiB RAM, 1 vCPU, 10-minute timeout.

The sync container (`docker/sync/src/main.py`) does:
1. Downloads blend outputs from `gs://{region_bucket}/output/blend/{date}/`
2. Optionally syncs to Google Drive (controlled by `ENABLE_DRIVE` env var)
3. Writes the current date to `gs://{region_bucket}/latest.txt`

Writing `latest.txt` is the critical final step - it is the regional dedup signal that pipeline-state checks at the top of the next scheduler invocation. If the pipeline fails at any earlier stage, `latest.txt` is not updated, so the next scheduler tick will retry the unfinished work for the same date.

### 10. log_complete / return_result

**Service: Cloud Logging + Cloud Workflows**

Returns the dates used or skipped by each stage:
```json
{
  "status": "completed",
  "region": "india",
  "aifs_ic_date": "20260421T00",
  "neuralgcm_ic_date": "20260421T00",
  "blend_date": "20260421T00",
  "sync_date": "20260421T00"
}
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
        +-- pipeline-state
        |     probes latest ICs, common bucket ICs, regional outputs,
        |     blend readiness, and sync status
        |
        +-- check_action ("check") --> return_checked
        |
        +-- run_models [parallel]
        |       |
        |       +-- AIFS branch
        |       |     downloads missing ECMWF ICs to common bucket
        |       |     Cloud Batch Jobs: aifs-{region}-{date}, aifs-ens-{region}-{date}
        |       |     raw forecasts -> common bucket; post-processed outputs -> regional bucket
        |       |     polls every 60s
        |       |
        |       +-- NeuralGCM branch
        |             downloads missing NCEP ICs to common bucket
        |             Cloud Batch Job: neuralgcm-{region}-{date}
        |             raw forecasts -> common bucket; post-processed outputs -> regional bucket
        |             polls every 120s
        |
        +-- pipeline-state
        |     finds latest blendable date
        |
        +-- blend
        |     Cloud Run Job: blend
        |     downloads configured inputs, runs eligible blends, uploads output/blend/{date}/
        |
        +-- sync
        |     Cloud Run Job: sync
        |     writes latest.txt in the regional bucket -> marks run complete for dedup
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
| Force-destroy data buckets | Yes | No |

---

## Logs

| What | Where |
|------|-------|
| Workflow step execution, variable values | Cloud Logging: resource type `workflows.googleapis.com/Workflow` (dev only - requires `LOG_ALL_CALLS`) |
| Cloud Run Job container stdout/stderr | Cloud Logging: resource type `run.googleapis.com/CloudRunJob` |
| Cloud Batch job container stdout/stderr | Cloud Logging: resource type `batch.googleapis.com/Job` |
| GenCast TPU VM startup and container stdout/stderr | Cloud Logging: log `monsoon-tpu-vm`, labels include `worker_hostname`, `node_id`, `queued_resource_id`, `attempt`, and `stream`; full log files are also uploaded under `intermediate/tpu-dispatch/.../logs/` |
| Workflow execution history | Cloud Workflows console -> Executions tab |
