# Cloud Pipeline Infrastructure

This directory contains the Terraform (OpenTofu) configuration for deploying the monsoon onset
prediction pipeline on Google Cloud Platform.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Initial Setup (Manual Steps)](#initial-setup-manual-steps)
- [Deploying Infrastructure](#deploying-infrastructure)
- [Staging Static Assets](#staging-static-assets)
- [Container Images](#container-images)
- [Infrastructure Overview](#infrastructure-overview)
- [Module Reference](#module-reference)
- [Extending the Infrastructure](#extending-the-infrastructure)

---

## Prerequisites

- [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install)
- [OpenTofu](https://opentofu.org/docs/intro/install/) (or Terraform ≥ 1.5)
- Docker (for local builds; Cloud Build is used in CI)
- Sufficient GCP permissions (Owner or a custom role with project-level IAM, compute, storage, and
  artifact registry access)

---

## Initial Setup (Manual Steps)

These steps must be done once before running Terraform. They cannot be automated because Terraform
itself needs a GCS bucket for remote state, and the Artifact Registry must exist before images can
be pushed.

### 1. Create a GCP Project

```bash
gcloud projects create <PROJECT_ID> --name="Monsoon Pipeline"
gcloud config set project <PROJECT_ID>
```

Link a billing account:

```bash
gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
```

### 2. Enable Required APIs

```bash
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  batch.googleapis.com \
  tpu.googleapis.com \
  workflows.googleapis.com \
  eventarc.googleapis.com \
  cloudscheduler.googleapis.com \
  containerfilesystem.googleapis.com \
  artifactregistry.googleapis.com \
  compute.googleapis.com \
  vpcaccess.googleapis.com \
  pubsub.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  storage.googleapis.com
```

### 3. Create the Terraform State Bucket

Terraform stores its state remotely in a GCS bucket. This bucket must exist before running
`terraform init`. The bucket name must be globally unique.

```bash
gcloud storage buckets create gs://<PROJECT_ID>-tf-state \
  --location=us-central1 \
  --uniform-bucket-level-access
```

Enable versioning so you can recover from accidental state corruption:

```bash
gcloud storage buckets update gs://<PROJECT_ID>-tf-state --versioning
```

### 4. Authenticate for Terraform

```bash
gcloud auth application-default login
```

### 5. Build and Push Container Images

Container images must exist in Artifact Registry before Terraform can reference them in Cloud Run
and Cloud Batch resources. Use Cloud Build to build all images:

```bash
# From the repository root
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_REPO=monsoon-dev-containers \
  --project=<PROJECT_ID>
```

The `_REPO` substitution must match the Artifact Registry repository name that Terraform will
create. For `dev`, this is `monsoon-dev-containers`; for `prod`, `monsoon-prod-containers`.

> **Note:** The Artifact Registry repository is created by the `storage` Terraform module. On a
> brand-new project, you must run `terraform apply -target=module.storage` first (see step 7), then
> build and push images, then apply the rest.

---

## Deploying Infrastructure

### 6. Configure Terraform Variables

Each environment has its own directory under `environments/`. Copy or create a `terraform.tfvars`
file in the environment directory you want to deploy:

```bash
cd terraform/environments/dev
```

Create `terraform.tfvars`:

```hcl
project_id = "<PROJECT_ID>"
region     = "us-central1"
```

All other variables have defaults defined in `main.tf` for each environment (e.g., `dev` uses
preemptible GPUs, 30-day retention, 6-hour pipeline schedule).

### 7. Initialize Terraform

Pass the state bucket created in step 3:

```bash
tf init -backend-config="bucket=<PROJECT_ID>-tf-state"
```

### 8. Plan

```bash
tf plan -var-file=terraform.tfvars
```

Review the plan output carefully before applying. On a fresh project, expect ~30–40 resources.

### 9. Apply

```bash
tf apply -var-file=terraform.tfvars
```

Type `yes` to confirm. Apply typically takes 3–5 minutes. Resources are created in dependency
order: networking → storage → compute → orchestration → monitoring.

### 10. Verify

After apply completes, check the outputs:

```bash
tf output
```

Expected outputs:
- `common_bucket` — common GCS bucket name (ICs, weights, full-field, intermediate markers)
- `region_buckets` — map of forecast region to its output bucket name
- `workflow_url` — Cloud Workflows execution endpoint

Verify the Cloud Scheduler jobs and Workflow were created in the GCP console or via:

```bash
gcloud workflows list --project=<PROJECT_ID> --location=us-central1
gcloud scheduler jobs list --project=<PROJECT_ID> --location=us-central1
```

---

## Staging Static Assets

After `tf apply` succeeds and before the first pipeline run, model weights and blend support
files must be uploaded once. These files are not stored in the repository (size or licensing
constraints) and are not created by Terraform.

There is **no separate weights bucket**. The storage module provisions a single **common bucket**
(`monsoon-{env}-common-{project_id}`) plus one output bucket per region; all static assets are
staged under the common bucket's **`weights/` prefix**, which every container reads at runtime via
`GCS_COMMON_BUCKET`. Look up the common bucket name from the environment's Terraform output:

```bash
cd terraform/environments/dev
COMMON_BUCKET=$(tf output -raw common_bucket)   # e.g. monsoon-dev-common-<PROJECT_ID>
```

The objects below mirror the repo-relative paths the science scripts expect. The lists are
verified against each container's `docker/<image>/src/main.py` download logic.

### AIFS model weights and sparse transform matrices

Only the **v2** AIFS variants run in the cloud (`config/models.json`: `aifs-v2` and `aifs-ens-v2`
have `"run": "true"`; the v1 entries are `"false"`). Both the `aifs-v2` and `aifs-ens-v2`
containers read their checkpoint filename from `config/models.json` and download it from
`weights/aifs/<filename>`, plus the two shared sparse-interpolation matrices. The science scripts
load these relative to `AIFS/utils/` (`../weights/` and `../EKR/mir_16_linear/`).

| File | Common-bucket path |
|---|---|
| AIFS v2 checkpoint | `weights/aifs/aifs-single-mse-2.0.ckpt` |
| AIFS-ENS v2 checkpoint | `weights/aifs/aifs-ens-crps-2.0.ckpt` |
| Sparse interpolation matrix (used by `preprocess_ic.py`) | `weights/aifs/EKR/mir_16_linear/9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz` |
| Sparse interpolation matrix (used by `run_model*.py`) | `weights/aifs/EKR/mir_16_linear/7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz` |

```bash
gcloud storage cp aifs-single-mse-2.0.ckpt gs://${COMMON_BUCKET}/weights/aifs/aifs-single-mse-2.0.ckpt

gcloud storage cp aifs-ens-crps-2.0.ckpt gs://${COMMON_BUCKET}/weights/aifs/aifs-ens-crps-2.0.ckpt

gcloud storage cp 9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz \
  gs://${COMMON_BUCKET}/weights/aifs/EKR/mir_16_linear/9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz

gcloud storage cp 7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz \
  gs://${COMMON_BUCKET}/weights/aifs/EKR/mir_16_linear/7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz
```

> The two `.npz` matrices are not in the repo. If you don't have them locally, run the AIFS science
> stack once (or the `aifs-v2` container) against any date — `earthkit-regrid`/anemoi generates
> them under `AIFS/EKR/mir_16_linear/` — then upload the two files named above.

### NeuralGCM model weights and forcing

The `neuralgcm` container downloads exactly two static files: the model checkpoint and the
SST/sea-ice climatology forcing. (No ERA5 reference grid is needed; that file is no longer used.)

| File | Common-bucket path |
|---|---|
| NeuralGCM model checkpoint | `weights/neuralgcm/models_v1_precip_stochastic_precip_2_8_deg.pkl` |
| SST/sea-ice climatology forcing | `weights/neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc` |

```bash
gcloud storage cp models_v1_precip_stochastic_precip_2_8_deg.pkl \
  gs://${COMMON_BUCKET}/weights/neuralgcm/models_v1_precip_stochastic_precip_2_8_deg.pkl

gcloud storage cp SST-SeaIce_clim_1979_2017_no_leap.nc \
  gs://${COMMON_BUCKET}/weights/neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc
```

### GenCast (no staging required)

GenCast pulls its weights and normalization statistics directly from the **public DeepMind
bucket** at runtime, so there is nothing for the operator to stage:

- `gs://dm_graphcast/gencast/params/GenCast 0p25deg Operational <2022.npz`
- `gs://dm_graphcast/gencast/stats/{diffs_stddev,mean,stddev,min}_by_level.nc`

Its only per-run input, the SST initial condition, is produced by the downloader job at
`ic/gencast_sst/{date}/sst_{date}.nc` in the common bucket during each pipeline run.

### Blend support files (per region)

Before running a region's blend, the `blend` container mirrors the **entire** prefix
`gs://${COMMON_BUCKET}/weights/blend/{region}/` into `/app/blend/data/`, preserving the object
layout, so the science scripts can use their repo-local paths. Stage one tree per forecast region
(`india`, `ethiopia`) containing that region's support and coefficient files (thresholds,
climatology, sub-district/grid regridding weights, shapefiles, trained model coefficients, etc.) —
i.e. the large or environment-specific files that are gitignored under
`blend/utils/{region}2026/.../data/`. Small support files and coefficients that are committed to
the repo are already baked into the image and do not need staging.

```bash
# Upload each region's support tree (layout under the prefix is mirrored verbatim into blend/data/)
gcloud storage cp -r india/    gs://${COMMON_BUCKET}/weights/blend/india/
gcloud storage cp -r ethiopia/ gs://${COMMON_BUCKET}/weights/blend/ethiopia/
```

> If `weights/blend/{region}/` is empty, the blend job logs a warning and then fails when a script
> reaches for a missing support file — so stage these before the first run for each region you
> enable in `forecast_regions`.

---

## Container Images

The pipeline images are built from the `docker/` directory at the repo root.
Cloud Build builds them in parallel using `cloudbuild.yaml`.

`cloudbuild.yaml` builds nine images (the build/push of each is gated by `.image-deps.yaml`, which
maps source paths to the images they affect):

| Image | Dockerfile | Target | Description |
|---|---|---|---|
| `monsoon-downloader` | `docker/downloader/Dockerfile` | Cloud Run Job | Downloads ECMWF and NCEP/GDAS initial conditions (+ GenCast SST) |
| `monsoon-pipeline-state` | `docker/pipeline-state/Dockerfile` | Cloud Run Service | Inspects bucket state and the latest available ICs; queried by the workflow |
| `monsoon-tpu-dispatch` | `docker/tpu-dispatch/Dockerfile` | Cloud Run Job | Creates and polls the GenCast TPU queued resource |
| `monsoon-sync` | `docker/sync/Dockerfile` | Cloud Run Job | Syncs final outputs to the operational web repository / Google Drive |
| `monsoon-blend` | `docker/blend/Dockerfile` | Cloud Batch | Blends model outputs, applies the trained model, generates diagnostics |
| `monsoon-aifs-v2` | `docker/aifs-v2/Dockerfile` | Cloud Batch | Runs AIFS v2 deterministic GPU inference |
| `monsoon-aifs-ens-v2` | `docker/aifs-ens-v2/Dockerfile` | Cloud Batch | Runs AIFS-ENS v2 GPU inference |
| `monsoon-neuralgcm` | `docker/neuralgcm/Dockerfile` | Cloud Batch | Runs NeuralGCM GPU inference |
| `monsoon-gencast` | `docker/gencast/Dockerfile` | Cloud TPU VM | Runs GenCast TPU inference |

(The `docker/aifs/` (v1) and `docker/postprocess/` directories exist in the tree but are not built
by `cloudbuild.yaml`; the cloud pipeline runs the v2 AIFS variants only.)

### Build Context

All Dockerfiles use the **repository root** as the Docker build context. This allows Dockerfiles to
`COPY` from sibling directories (e.g., the `downloader` image copies the `IC/utils/` science
scripts — `download_ecmwf.py` and `download_ncep.py` — directly into the container). Do not change
to the `docker/<name>/` directory before building — always build from the repo root with
`-f docker/<name>/Dockerfile .`.

### Image Naming

Images are tagged with both a short commit SHA and `latest`:

```
<REGION>-docker.pkg.dev/<PROJECT_ID>/<REPO>/<NAME>:<SHORT_SHA>
<REGION>-docker.pkg.dev/<PROJECT_ID>/<REPO>/<NAME>:latest
```

Terraform resources reference the `latest` tag. The `lifecycle { ignore_changes }` block on each
Cloud Run Job prevents Terraform from reverting image updates that happen outside of a `tf apply`
(i.e., after a Cloud Build run).

---

## Infrastructure Overview

The infrastructure is organized into five Terraform modules. Resources follow the naming convention
`monsoon-{environment}-{resource}`.

```
Cloud Scheduler (per forecast region)
    │
    ▼
Cloud Workflows (main pipeline)  ──queries──►  Cloud Run Service: pipeline-state
    │
    ├── Cloud Run Job: downloader      (ECMWF + NCEP + GenCast SST, parallel)
    │
    ├── Cloud Batch Job: aifs-v2       (GPU inference, parallel)
    ├── Cloud Batch Job: aifs-ens-v2   (GPU inference, parallel)
    ├── Cloud Batch Job: neuralgcm     (GPU inference, parallel)
    ├── Cloud Run Job: tpu-dispatch ──► TPU queued resource: gencast (v5p-64, parallel)
    │
    ├── Cloud Batch Job: blend         (CPU, event-driven when inputs are ready)
    └── Cloud Run Job: sync            (sequential)
```

### Networking (`modules/networking`)

- **VPC** with a single regional subnet (`10.x.x.0/24`), private Google access enabled
- **Cloud NAT** for outbound internet access (downloading external forecast data) without public IPs
- **Firewall rules**: internal VPC traffic, IAP SSH (port 22 from `35.235.240.0/20`), and Google
  health check ranges
### Storage (`modules/storage`)

- **Common bucket** (`monsoon-{env}-common-{project_id}`): the single shared bucket. It holds
  staged model weights and blend supports (`weights/`), shared initial conditions (`ic/`),
  full-field raw model forecasts (`full_field/`), JAX compilation cache (`jax-cache/`), and
  intermediate markers/latest-date files (`intermediate/`). There is **no** separate weights
  bucket. Lifecycle rules delete `ic/`, `intermediate/`, and `jax-cache/` objects after
  `retention_days`; `full_field/` objects optionally transition to Nearline after
  `archive_after_days`. Versioning follows `enable_versioning`.
- **Regional output buckets** (`monsoon-{env}-{region}-{project_id}`): one bucket per
  `forecast_regions` entry, holding only post-processed model products and blend outputs under
  `output/`.
- **Artifact Registry** (`monsoon-{env}-containers`): stores pipeline container images. Dev
  repositories delete old image versions after `artifact_registry_cleanup_older_than`, which
  defaults to 7 days, while retaining the 3 most recent versions per package.
- **Pipeline service account** with `storage.objectAdmin` on the common and regional buckets and
  `artifactregistry.reader` on the registry.
- **GCS folder structure**: common-bucket prefixes are `weights/`, `ic/`, `full_field/`,
  `intermediate/`, and `jax-cache/`; regional buckets use `output/`.

### Compute (`modules/compute`)

Creates Cloud Run v2 **Jobs** for the batch workloads plus one Cloud Run **Service**
(`pipeline-state`) the workflow queries for state:

| Resource | Type | Memory | CPU | Timeout |
|---|---|---|---|---|
| downloader | Job | 4 Gi | 2 | 900 s |
| sync | Job | 1 Gi | 1 | 600 s |
| tpu-dispatch | Job | 1 Gi | 1 | 86400 s |
| pipeline-state | Service | (default) | — | — |

All Cloud Run resources:
- Run under the pipeline service account
- Receive `ENVIRONMENT`, `GCS_COMMON_BUCKET`, `GCS_REGION_BUCKETS` (JSON map), `REGIONS`,
  `REGION_MODELS`, and `PROJECT_ID` as environment variables (the workflow passes per-execution
  values such as `DATE` and `FORECAST_REGION(S)` at run time; `sync` also gets `ENABLE_DRIVE` and
  `MONSOON_CLUSTER`).

GPU inference (AIFS v2, AIFS-ENS v2, NeuralGCM) and CPU blend/diagnostics run on **Cloud Batch**.
Blend uses `e2-highmem-4` by default; GPU stages default to a machine type derived from
`gpu_type`, overridable per stage via `batch_model_resources`. GenCast runs separately on a
**Cloud TPU queued resource** using TPU v5p-64 (`2x4x4`) and the `ct5p-hightpu-4t` TPU VM host
type, created and polled by the `tpu-dispatch` job.

### Orchestration (`modules/orchestration`)

- **Cloud Workflows** (`monsoon-{env}-pipeline`): a YAML workflow that drives the full pipeline.
  See `modules/orchestration/workflow.yaml.tpl` for the full step definition.
  - Accepts `region` and optionally `date` as inputs
  - Calls the pipeline-state service to discover latest ICs and check common/regional bucket state
  - Downloads missing ECMWF and NCEP ICs
  - Submits deterministic AIFS, AIFS-ENS, NeuralGCM, blend, and diagnostics as idempotent Cloud Batch jobs with stable IDs
  - Runs GenCast as a TPU queued resource with a stable `gencast-{date}` ID
  - Advances downstream stages from common-bucket `intermediate/` object finalization events
- **Cloud Scheduler jobs** (one per `forecast_regions` entry): trigger the Workflow on
  `pipeline_schedule` (default every 6 hours in dev, configurable in prod)
- **Pub/Sub topic**: `pipeline-triggers` receives common-bucket `intermediate/` object finalization
  notifications and routes them through Eventarc to the workflow. The workflow advances only for
  `intermediate/*_done` marker objects.
- **Workflow service account** with `run.developer`, `batch.jobsEditor`, and `logging.logWriter`,
  plus `iam.serviceAccountUser` to attach the pipeline service account to jobs

### Monitoring (`modules/monitoring`)

- Cloud Monitoring resources scoped to the environment
- Alerting disabled in dev (`enable_alerts = false`); configure `notification_emails` in prod

---

## Extending the Infrastructure

### Adding a New Forecast Region

1. Add the region name to `forecast_regions` in the environment's `main.tf`:
   ```hcl
   locals {
     forecast_regions = ["india", "west_africa"]
   }
   ```
2. Run `tf apply`. The storage module will create the GCS folder structure for the new region,
   and the orchestration module will create a new Cloud Scheduler job for it.

No other changes are needed — the workflow is parameterized by region.

### Adding a New Pipeline Stage

1. Write the Dockerfile in `docker/<stage>/Dockerfile`, using the repo root as build context.
2. Add a build and push step to `cloudbuild.yaml` (copy an existing step, update the `id` and
   image name).
3. Add the image variable to `modules/compute/variables.tf` and the Cloud Run Job definition to
   `modules/compute/main.tf`.
4. Pass the image URL in each environment's `main.tf` (following the existing pattern using
   `module.storage.artifact_registry_url`).
5. Add the workflow step in `modules/orchestration/workflow.yaml.tpl` and update
   `modules/orchestration/variables.tf` to thread the new job name through.
6. Build images with Cloud Build, then run `tf apply`.

### Adding a New Environment

1. Copy `environments/dev/` to `environments/<name>/`.
2. Update the `locals` block (environment name, schedule, retention settings).
3. Update the `terraform` backend block prefix: `prefix = "monsoon/<name>"`.
4. Run `tf init -backend-config="bucket=<PROJECT_ID>-tf-state"` and `tf apply`.

### Modifying Resource Sizes

Cloud Run Job memory/CPU and Cloud Batch machine types are set in `modules/compute/main.tf` and
`modules/compute/variables.tf`. To change them per environment, add override variables to the
environment's `main.tf` and pass them into the module. For Batch stages, use
`batch_model_resources` to override per-stage VM settings such as `machine_type`,
`boot_disk_size_gb`, `boot_disk_type`, `cpu_milli`, `memory_mib`, `gpu_type`, `gpu_count`,
`install_gpu_drivers`, `max_run_duration`, `mount_common_bucket`, `gcs_mount_options`, and
`provisioning_model`.
Unset CPU, memory, and GPU fields use known machine-type defaults when available. For GenCast TPU
placement, set `gencast_tpu_zone`; it defaults to
`us-central1-a` and supports `us-east5-a` when TPU capacity must move east.
FUSE-backed full-field writer stages default to Cloud Storage FUSE's `aiml-checkpointing`
profile. Read-heavy blend and diagnostics stages use a bounded `/tmp/gcsfuse-cache`
file cache with parallel downloads enabled.

### Updating Container Images Without Terraform

After a `gcloud builds submit`, new images are tagged `latest` and pushed to Artifact Registry.
Cloud Run Jobs pick up the new image on the next execution automatically — no `tf apply` needed.
The `lifecycle { ignore_changes }` block on each job ensures Terraform won't overwrite your latest
push on the next plan/apply.
