# Cloud Pipeline Architecture

End-to-end reference for the GCP-based monsoon onset forecast pipeline. Covers every Google Cloud service involved, how they connect, and the exact sequence of execution.

---

## Current India pilot (Oct 2026)

This is what runs operationally today. Values come from
`terraform/environments/dev/main.tf` and the Terraform modules; the containers named below
hold the logic. Some of the generic reference sections further down predate this setup.

### What is switched on

The pilot runs in the **dev** environment. Prod sets no DryWetCast image, so it has no
DryWetCast job.

| Region | Models | Stages | Drive sync rules |
|--------|--------|--------|------------------|
| india | `AIFS_ENS_v2` | `drywetcast`, `sync` | `drywetcast` |
| ethiopia | none | `sync` (no rules left) | none |

`disabled_models` switches off `gencast`, `AIFS_single_v2` and `neuralgcm` everywhere.
`disabled_region_models` switches off `AIFS_ENS_v2` for ethiopia. `disabled_stages` switches
off india's `model_diagnostics` and ethiopia's `blend` and `model_diagnostics`.

### Daily flow

1. **ECMWF ICs.** A scheduled pass finds the latest 00z ECMWF cycle through pipeline-state.
   The `monsoon-dev-downloader` Cloud Run job copies the GRIBs to
   `gs://monsoon-dev-common-<project>/ic/ecmwf/<D>/grib/`.
2. **Ensemble.** The workflow submits the Batch job `aifs-ens-v2-<YYYYMMDD>-00`.
   - Image: `monsoon-aifs-ens-v2`.
   - Machine: `a2-highgpu-2g` (2 × A100), 24 vCPU, 170 GiB, STANDARD provisioning.
   - Limits: 7200 s maximum run time; at most `ensemble_max_attempts` = 3 attempts per date.
   - Settings: `AIFS_ENS_N_MEMBERS=51`, `AIFS_ENS_LEAD_TIME_HOURS=168`.
     `run_model_ENS.py` runs one worker per visible GPU.
   - Output: the India post-processed products and the DryWetCast input store go to the
     india bucket, and the full field goes to the common bucket. Last, the job writes
     `intermediate/AIFS_ENS_v2_india_<D>_done`.
3. **DryWetCast.** Once the done marker and the store's `.zmetadata` exist, pipeline-state
   asks for a DryWetCast run and the workflow starts the `monsoon-dev-drywetcast` Cloud Run
   job.
   - Job limits: 4 vCPU, 8 GiB, 3600 s, no Cloud Run retries.
   - A claim object stops two passes from running the same date.
   - Order: the two GEFS configs run first. Each is uploaded and marked done straight away.
   - NCMRWF configs: the job polls the NCMRWF portal for up to 20 min
     (`NCMRWF_WAIT_MINUTES`). If the file isn't there yet, the configs stay pending for a
     later pass. A failed download is logged as a WARNING, its partial files are cleared,
     and it is retried once after 60 s and then at each 5-min poll. The configs fail only if
     the download is still failing when the window ends.
   - From the cutoff, `drywetcast_ncmrwf_cutoff_utc` = 14:00 UTC (19:30 IST), a pass makes
     one last check. Missing NCMRWF configs are then marked `ncmrwf_unavailable`.
   - A config that fails 3 times (`DRYWETCAST_MAX_ATTEMPTS`) is not retried.
4. **Drive delivery.** The `drywetcast` sync rule (`drywetcast_drive_sync = true`) mirrors
   each finished config, `output/drywetcast/<D>/<config>/`, to `<D>/<config>/` in the Drive
   folder set by `drive_folder_id` in the dev `main.tf`. The date folders sit directly in
   that folder. The sync job runs fire-and-forget, so a Drive failure never holds up a pass.

Every new object under `intermediate/` in the common bucket, such as a done marker, also
starts a workflow pass, through Pub/Sub and Eventarc. The scheduled passes cover everything
else.

### Schedule

Every Cloud Scheduler job uses `time_zone = "UTC"`. IST is UTC+5:30.

| Scheduler job | Cron (UTC) | UTC | IST | Purpose |
|---------------|------------|-----|-----|---------|
| `monsoon-dev-pipeline-trigger-early` | `*/5 7 * * *` | 07:00–07:55, every 5 min | 12:30–13:25 | Start the ensemble as soon as ECMWF 00z is published |
| `monsoon-dev-pipeline-trigger` | `0 8-14 * * *` | 08:00–14:00, hourly | 13:30–19:30 | Main passes; the 14:00 pass makes the NCMRWF cutoff check |
| `monsoon-dev-pipeline-trigger-ncmrwf-half` | `30 8-13 * * *` | 08:30–13:30, hourly | 14:00–19:00 | With the hourly passes, an NCMRWF poll can start every 30 min |
| `monsoon-dev-delivery-check` | `0 10 * * *` | 10:00 | 15:30 | Logs `DELIVERY_LATE` if today's India ensemble marker is missing (`india_ensemble_deadline_utc`) |

`scheduler_paused = true` pauses all of them. Workflow runs can still be started by hand.

### Buckets and output paths

`<D>` is the forecast date as `YYYYMMDDT00` (for example `20261014T00`), and `<YYYYMMDD>` is
its first 8 characters.

**Common bucket: `gs://monsoon-dev-common-<project>/`**

| Path | Contents |
|------|----------|
| `ic/ecmwf/<D>/grib/` | ECMWF initial conditions |
| `full_field/AIFS_ENS_v2/<D>/init_<D>.zarr` | Ensemble full field |
| `intermediate/AIFS_ENS_v2_india_<D>_done` | Ensemble done for India |
| `intermediate/drywetcast_india_<config>_<D>_done` | One DryWetCast config done |
| `intermediate/drywetcast_india_<D>_done` | All four configs done |
| `weights/drywetcast/subdistrict/v1/` | Subdistrict input bundle (below) |

**India bucket: `gs://monsoon-dev-india-<project>/`**

| Path | Contents |
|------|----------|
| `output/AIFS_ENS_v2/<D>/` | Ensemble post-processed products for India |
| `output/AIFS_ENS_v2/<D>/AIFS_ENS_v2/drywetcast/init_<YYYYMMDD>T00.zarr` | DryWetCast input store |
| `output/drywetcast/<D>/<config>/<YYYYMMDD>_00.png` | Forecast map |
| `output/drywetcast/<D>/<config>/<YYYYMMDD>_00.npz` | Forecast data |
| `output/drywetcast/<D>/<config>/<YYYYMMDD>_00_outlook.png` | Outlook map |
| `output/drywetcast/<D>/<config>/<YYYYMMDD>_00_subdistrict.pkl` and `_subdistrict.png` | Subdistrict products |
| `output/drywetcast/<D>/manifest.json` | Run summary per config, including subdistrict state |
| `output/drywetcast/archive/<config>/<YYYYMMDD>_00.npz` | Per-config archive |
| `drywetcast-state/<D>/claim.json`, `status.json` | DryWetCast claim, and per-config state and attempts |

`<config>` is one of `gefs_reduced`, `gefs_full`, `ncmrwf_reduced` or `ncmrwf_full`.

### Subdistrict bundle

- **Input:** the bundle is at `gs://monsoon-dev-common-<project>/weights/drywetcast/subdistrict/v1/`
  (`DRYWETCAST_SUBDISTRICT_GCS`).
  - It must contain `subdistrict_manifest.tsv` plus the files DryWetCast's
    `pipeline/subdistrict.py` requires at `22cdbe5`. These are `source_to_index.pkl`,
    `target_to_index.pkl`, `weight_matrix.npy`, and the `Sub_districts_India_ESRI` and
    `STATE_BOUNDARY` shapefiles (`.shp`, `.shx`, `.dbf`, `.prj`).
  - The job copies the bundle once per run and checks every file against the manifest
    before DryWetCast sees it.
- **Output:** each config's `<YYYYMMDD>_00_subdistrict.pkl` and `.png` sit in
  `output/drywetcast/<D>/<config>/` in the india bucket, and in `<D>/<config>/` on Drive.
- **Failures don't stop delivery:**
  - If the bundle fails to copy or verify, every config runs without it.
  - If DryWetCast fails with the bundle, that config is rerun without it.
  - Either way the job logs `DRYWETCAST_SUBDISTRICT_UNAVAILABLE` or
    `DRYWETCAST_SUBDISTRICT_FAILED` at ERROR, and `manifest.json` records what happened
    (`subdistrict_bundle`, `this_run.<config>.subdistrict`).

### Alerts

`enable_alerts = true` and `enable_delivery_alert = true` in dev. Alerts are emailed to
`notification_emails` in the dev `main.tf`.

| Policy | Fires on | Rate limit |
|--------|----------|------------|
| `[DEV] Monsoon Delivery Late` | Workflow log `DELIVERY_LATE` (the delivery check) or `BATCH_RETRY_CAP` (a Batch job used all its attempts) | 1 per hour |
| `[DEV] DryWetCast Subdistrict Products Missing` | `DRYWETCAST_SUBDISTRICT_UNAVAILABLE` or `DRYWETCAST_SUBDISTRICT_FAILED` from the drywetcast job | 1 per hour |
| `[DEV] Monsoon Sync Failing Repeatedly` | `SYNC_ITEMS_GAVE_UP` from the sync job (an item failed in its maximum number of executions) | 1 per hour |
| `[DEV] Monsoon Pipeline Failure` | Any workflow log at ERROR or above | 1 per 5 min |
| `[DEV] Cloud Run Job Failure` | A failed Cloud Run job execution (any job) | none set |

`[PROD] Monsoon Pipeline Stale` exists only in prod.

---

## Google Cloud Services Used

| Service | Role |
|---------|------|
| Cloud Scheduler | Cron trigger - fires the workflow on a schedule |
| Cloud Workflows | Orchestrator - submits ready work from scheduler and marker events |
| Cloud Run Jobs | Lightweight compute for downloader, sync, and TPU dispatch |
| Cloud Batch | GPU model compute and CPU blend/diagnostics compute |
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
    dev/main.tf       <- dev-specific variables (the India pilot; see Current India pilot above)
    prod/main.tf      <- production configuration
  modules/
    networking/       <- VPC, subnet, Cloud NAT, firewall rules
    storage/          <- GCS buckets, Artifact Registry, pipeline service account
    compute/          <- Cloud Run Jobs, IC checker service, Batch job template
    orchestration/    <- Cloud Workflows, Cloud Scheduler, Pub/Sub topics, workflow SA
    monitoring/       <- Alerting
```

### Buckets

One common bucket and one bucket per forecast region are created per environment. There is no
separate weights bucket — staged weights live under the common bucket's `weights/` prefix.

**`monsoon-{env}-common-{project_id}`** (common bucket)

Shared storage for staged static assets, initial conditions, intermediate markers, and full-field raw model forecasts. These paths are not region-prefixed:

```
weights/
  aifs/...                                  <- staged AIFS v2 checkpoints + sparse matrices
  neuralgcm/...                             <- staged NeuralGCM checkpoint + forcing
  blend/{region}/...                        <- staged per-region blend support files
ic/
  ecmwf/{date}/grib/*.grib2                 <- ECMWF IFS GRIB initial conditions
  gencast_sst/{date}/sst_{date}.nc          <- GenCast SST initial condition
  ncep/{date}/gdas_{date}.pgrb2             <- NCEP GDAS initial conditions
full_field/
  aifs/{date}/init_{date}.nc                <- AIFS full-field forecast
  aifs_ens/{date}/init_{date}.zarr/         <- AIFS-ENS full-field forecast
  neuralgcm/{date}.zarr/                    <- NeuralGCM full-field ensemble forecast
  gencast/{date}/init_{date}.zarr/          <- GenCast full-field forecast
intermediate/
  latest_date.txt                           <- latest date marker written by downloader
  latest_ecmwf_date.txt                     <- actual ECMWF date written by downloader
  {model}_{region}_{date}_done              <- per-(model, region) completion markers
jax-cache/                                  <- persistent JAX compilation cache
```

`ic/`, `intermediate/`, and `jax-cache/` objects expire after `retention_days` (30 in dev) per lifecycle rules; `weights/` is retained, and `full_field/` is retained and may transition to Nearline.

**`monsoon-{env}-{region}-{project_id}`** (regional output buckets)

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

**Staged static assets** (`weights/` prefix of the common bucket — no separate weights bucket)

Read-only files uploaded once before the first run (see
[terraform/README.md → Staging Static Assets](../terraform/README.md#staging-static-assets) for the
upload commands). Grids and masks are baked into the container images, not staged here.

```
weights/aifs/
  aifs-single-mse-2.0.ckpt                   <- AIFS v2 checkpoint
  aifs-ens-crps-2.0.ckpt                     <- AIFS-ENS v2 checkpoint
  EKR/mir_16_linear/{hash}.npz               <- sparse IFS->N320 transform matrices (×2)
weights/neuralgcm/
  models_v1_precip_stochastic_precip_2_8_deg.pkl  <- NeuralGCM checkpoint
  forcings/SST-SeaIce_clim_1979_2017_no_leap.nc   <- SST/sea ice climatology
weights/blend/{region}/                      <- per-region blend support + coefficient files
```

GenCast weights and stats are **not** staged — the `gencast` container reads them from the public
`gs://dm_graphcast` bucket at runtime.

### Container Images

All images are stored in a single **Artifact Registry** Docker repository (`monsoon-{env}-containers`) in the same region. The pipeline service account has `roles/artifactregistry.reader` on this repository. Images are referenced by tag (`latest`) - Terraform ignores image changes in the Cloud Run Job definitions so containers can be updated independently via `docker push` without a `tofu apply`. Dev repositories delete old image versions after 7 days by default.

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

**Service: Cloud Scheduler** (`monsoon-{env}-pipeline-trigger`, plus any `extra_pipeline_schedules`)

Runs on a cron schedule (see [Schedule](#schedule) for dev; `*/15 * * * *` in prod). Sends an HTTP POST to the Cloud Workflows Executions API:

```
POST https://workflowexecutions.googleapis.com/v1/projects/.../workflows/monsoon-{env}-pipeline/executions
```

Body:
```json
{
  "argument": "{}"
}
```

`region`, `date` and `action` are intentionally omitted - one execution handles every region and the workflow detects the latest date itself. Authentication uses the workflow SA's OAuth2 token with scope `cloud-platform`.

### 2. init

**Service: Cloud Workflows (assign step)**

Initializes variables from the `args` map:

- `region` - read directly from `args.region` (always present)
- `requested_date` - `default(map.get(args, "date"), "")` - empty string if not provided by scheduler
- `action` - `default(map.get(args, "action"), "run")` - defaults to `"run"`
- `project_id` - hardcoded from Terraform template substitution at deploy time
- `common_bucket` - `monsoon-{env}-common-{project_id}`, substituted at deploy time
- `region_bucket` - looked up from Terraform's `region_buckets` map for the requested forecast region

`map.get` safely handles missing keys (returns null); `default` converts null to the fallback. This avoids the `KeyError: key not found` that `args.date` would throw for absent keys.

### 3. call_pipeline_state_initial

**Service: Cloud Run Service** (`monsoon-{env}-pipeline-state`)

The workflow calls the pipeline-state service once at the start. If no date was supplied, the service probes external IC sources for the latest 00z ECMWF and NCEP cycles, then checks GCS for cached ICs, completed forecasts, blendable outputs, and pending sync work.

Each environment can disable models globally with `disabled_models` in its Terraform environment file. Model names accept hyphen or underscore spelling, so `aifs-ens` disables the internal `aifs_ens` model. The effective region config removes disabled models, dependent sync rules, and dependent stages before it is passed to Workflows and pipeline-state.

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
gs://{common_bucket}/ic/ecmwf/{ecmwf_date}/grib/
gs://{common_bucket}/ic/gencast_sst/{ecmwf_date}/sst_{ecmwf_date}.nc
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
gs://{common_bucket}/ic/ncep/{neuralgcm_ic_date}/gdas_{neuralgcm_ic_date}.pgrb2
```

Cloud Batch is used for model inference and blend/diagnostics. Model jobs need accelerators and
blend needs a cheap high-memory CPU VM.

**AIFS Batch jobs:**

Creates idempotent Cloud Batch jobs with stable IDs such as `aifs-single-v2-{date}` and
`aifs-ens-v2-{date}`. Pipeline-state passes the pending forecast regions to each model job and
skips region/model pairs that already have post-processed outputs.

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

The cloud runs the **v2** AIFS containers — `docker/aifs-v2/src/main.py` (deterministic,
`AIFS_single_v2`) and `docker/aifs-ens-v2/src/main.py` (ensemble, `AIFS_ENS_v2`). Each does:
1. Downloads ECMWF GRIB files from the common bucket under `ic/ecmwf/{date}/grib/`
2. Downloads its checkpoint — the filename is read from `config/models.json` (`aifs-single-mse-2.0.ckpt` / `aifs-ens-crps-2.0.ckpt`) — from `weights/aifs/` in the common bucket
3. Downloads the two sparse transform matrices (`.npz`) from `weights/aifs/EKR/mir_16_linear/` in the common bucket
4. Runs `run_model.py` (deterministic) or `run_model_ENS.py` (ensemble)
5. Runs `post_process.py` for each configured region
6. Uploads full-field forecasts to `full_field/` in the common bucket and post-processed products to `output/` in the regional bucket, then writes a `intermediate/{model}_{region}_{date}_done` marker

The `--model` / `MODEL` value is set per Batch job by the workflow.

The workflow submits Batch jobs with `skip_polling: true`. Completion is detected from GCS output
and common-bucket `intermediate/*_done` markers, which wake the workflow through
Pub/Sub/Eventarc.

**Branch B - run_neuralgcm:**

Creates a Cloud Batch job with ID `neuralgcm-{region}-{date}`. Spec:
- 1 task, 1 retry, max 3600s (1 hour)
- 48 vCPU, 340 GiB RAM
- 4 A100 GPUs by default so NeuralGCM members can run in parallel

The NeuralGCM container (`docker/neuralgcm/src/main.py`) does:
1. Downloads `gdas_{date}.pgrb2` from the common bucket under `ic/ncep/{date}/`
2. Downloads the NeuralGCM checkpoint (`.pkl`) and SST/sea ice forcing file from the common bucket under `weights/neuralgcm/`
3. Runs `preprocess_ic.py` - interpolation of GRIB2 to NetCDF in the format NeuralGCM expects
4. Runs `run_model.py` - 30-member stochastic ensemble, split across visible
   GPUs, with asynchronous local Zarr writes mirrored through GCS FUSE
5. Runs `post_process.py` - per-member SJI, TP, TCW computation
6. Runs `post_process_merge.py` - merges all 30 members into ensemble statistics
7. Mirrors full-field forecasts to `gs://{common_bucket}/full_field/neuralgcm/{date}.zarr/`
8. Uploads post-processed products to `gs://{region_bucket}/output/neuralgcm/{date}/`
9. Writes completion markers: `gs://{common_bucket}/intermediate/neuralgcm_{region}_{date}_done`

The workflow does not poll NeuralGCM directly. It advances when NeuralGCM writes regional output
and `intermediate/neuralgcm_{region}_{date}_done`.

**GenCast TPU queued resource:**

GenCast runs on a TPU v5p-64 slice with topology `2x4x4` (32 chips across 8 `ct5p-hightpu-4t` hosts). The default zone is `us-central1-a`; set `gencast_tpu_zone = "us-east5-a"` to move TPU capacity east. If the TPU zone is outside the primary region, Terraform creates a matching regional subnet and NAT on the existing VPC.

The workflow submits one queued resource per GenCast date, using a stable ID `gencast-{date}`. Duplicate submissions poll the existing queued resource. The startup script runs the GenCast container on every TPU VM host with JAX distributed initialization enabled. `run_gencast.py` logs and validates:
- global device count: `32`
- local device count per TPU VM: `4`
- process count: `8`

Only JAX process 0 publishes full-field and region outputs; the other TPU hosts participate in the distributed run and skip duplicate Zarr writes. The TPU startup script passes `GENCAST_OUTPUT_DIR=/mnt/disks/common/full_field/gencast/{date}` into the container. Process 0 mounts the common bucket inside the GenCast container with Cloud Storage FUSE, so the full-field Zarr is written directly to the common bucket instead of filling the container overlay. Distributed TPU runs also set `JAX_COMPILATION_CACHE_DIR=/mnt/disks/common/jax-cache/gencast/v5p-64`; every TPU worker mounts the same common bucket before the first compile so later runs can reuse JAX persistent compilation cache entries. The GenCast utility code does not enable these FUSE-backed paths unless the cloud wrapper or an operator sets the relevant environment variables, preserving the HPC local-output workflow.

### 7. blend

**Service: Cloud Batch** (`monsoon-blend` image)

`e2-highmem-4` by default: 4 vCPU, 32 GiB RAM, 60-minute max run duration.

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
6. Writes `gs://{common_bucket}/intermediate/blend_{region}_{date}_done` after upload, which
   wakes the event-driven workflow advancement path.

### 8. sync

**Service: Cloud Run Jobs** (`monsoon-{env}-sync`)

1 GiB RAM, 1 vCPU, 10-minute timeout.

The sync container (`docker/sync/src/main.py`) does:
1. Loads the requested sync rule names from Terraform and the rule definitions from `sync/config/sync.yaml`
2. Stages each rule's `gcs_prefix` from the region bucket into that rule's local sync path
3. Syncs the staged files to Google Drive (`ENABLE_DRIVE=true` in Terraform-managed Cloud Run jobs)
4. Writes the current date to `gs://{region_bucket}/latest.txt`

The sync job uses `MONSOON_CLUSTER=gcp-dev` in dev and `MONSOON_CLUSTER=gcp` in prod. When Drive OAuth files are not baked into the image, provide them through the existing `external_api_secrets` map using `GOOGLE_DRIVE_CREDENTIALS_JSON` and `GOOGLE_DRIVE_TOKEN_JSON`; the container writes them to `/app/sync/.auth/` at runtime.

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
        +-- submit_ready_work
        |       |
        |       +-- AIFS branch
        |       |     Cloud Batch Jobs: aifs-{region}-{date}, aifs-ens-{region}-{date}
        |       |     raw forecasts -> common bucket; post-processed outputs -> regional bucket
        |       |
        |       +-- NeuralGCM branch
        |             Cloud Batch Job: neuralgcm-{region}-{date}
        |             raw forecasts -> common bucket; post-processed outputs -> regional bucket
        |       |
        |       +-- Blend / diagnostics
        |             Cloud Batch Job: blend-{region}-{date}, diagnostics-{region}-{date}
        |
        +-- common bucket intermediate *_done marker finalization
        |     Pub/Sub notification -> Eventarc -> same workflow -> submit_ready_work
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
| Schedule | See [Schedule](#schedule) | Every 15 min |
| Workflow call log level | `LOG_ALL_CALLS` (full execution history in console) | `LOG_NONE` |
| GPU provisioning | SPOT (preemptible), except AIFS_ENS_v2 on STANDARD | STANDARD |
| Cloud Run Job deletion protection | Off | On |
| GCS retention | 30 days (raw/intermediate) | Configurable with NEARLINE archival |
| GCS versioning | Disabled | Enabled |
| Monitoring alerts | Enabled, including the delivery alert | Enabled |
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
