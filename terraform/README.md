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
  workflows.googleapis.com \
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
- `storage_bucket` — main GCS data bucket name
- `workflow_url` — Cloud Workflows execution endpoint

Verify the Cloud Scheduler jobs and Workflow were created in the GCP console or via:

```bash
gcloud workflows list --project=<PROJECT_ID> --location=us-central1
gcloud scheduler jobs list --project=<PROJECT_ID> --location=us-central1
```

---

## Staging Static Assets

After `tf apply` succeeds and before the first pipeline run, several static files must be
uploaded manually to the weights bucket. These files are not stored in the repository (size or
licensing constraints) and are not created by Terraform.

The weights bucket name follows the pattern `monsoon-{env}-weights-{project_id}`. You can look
it up with:

```bash
tf output -raw storage_bucket   # data bucket
# weights bucket is not an output — read it from the storage module or the GCS console
# name: monsoon-{env}-weights-{project_id}
```

### AIFS model weights and sparse transform matrices

The AIFS science scripts load files relative to `AIFS/utils/`, so the paths inside the container
are `../weights/` and `../EKR/mir_16_linear/`. The container shim downloads these from the weights
bucket at runtime using the paths below.

| File | Weights bucket path |
|---|---|
| AIFS checkpoint | `aifs/aifs-single-mse-1.1.ckpt` |
| AIFS-ENS checkpoint | `aifs/aifs-ens-crps-1.0.ckpt` |
| Sparse interpolation matrix (used by `download_ic.py`) | `aifs/EKR/mir_16_linear/9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz` |
| Sparse interpolation matrix (used by `run_model.py`) | `aifs/EKR/mir_16_linear/7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz` |

```bash
WEIGHTS_BUCKET="monsoon-dev-weights-<PROJECT_ID>"

gcloud storage cp aifs-single-mse-1.1.ckpt gs://${WEIGHTS_BUCKET}/aifs/aifs-single-mse-1.1.ckpt

gcloud storage cp aifs-ens-crps-1.0.ckpt gs://${WEIGHTS_BUCKET}/aifs/aifs-ens-crps-1.0.ckpt

gcloud storage cp 9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz \
  gs://${WEIGHTS_BUCKET}/aifs/EKR/mir_16_linear/9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz

gcloud storage cp 7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz \
  gs://${WEIGHTS_BUCKET}/aifs/EKR/mir_16_linear/7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz
```

### NeuralGCM model weights and reference data

| File | Weights bucket path |
|---|---|
| NeuralGCM model checkpoint | `neuralgcm/models_v1_precip_stochastic_precip_2_8_deg.pkl` |
| SST/sea-ice climatology forcing | `neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc` |
| ERA5 reference grid | `neuralgcm/data/ERA5_2018_05_16_00.nc` |

```bash
gcloud storage cp models_v1_precip_stochastic_precip_2_8_deg.pkl \
  gs://${WEIGHTS_BUCKET}/neuralgcm/models_v1_precip_stochastic_precip_2_8_deg.pkl

gcloud storage cp SST-SeaIce_clim_1979_2017_no_leap.nc \
  gs://${WEIGHTS_BUCKET}/neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc

gcloud storage cp ERA5_2018_05_16_00.nc \
  gs://${WEIGHTS_BUCKET}/neuralgcm/data/ERA5_2018_05_16_00.nc
```

### Blend climatology CSVs

The blend step requires two large climatology CSV files that are too large to bundle in the
container image. They are downloaded from the weights bucket at runtime into
`/app/blend/data/support/large/` inside the container.

The small support files (`thresholds_df.csv`, `onset_clusters.csv`, `allowed_cells.csv`,
`all_cells.csv`, `exclude_cells.csv`) and the model coefficient files under `blend/data/coefs/`
are already bundled in the image and do not need to be staged.

| File | Weights bucket path |
|---|---|
| Ensemble climatology (standard) | `blend/support/large/ensemble_outputs_clim_2025.csv` |
| Ensemble climatology (Morak onset) | `blend/support/large/ensemble_outputs_clim_2025_mok.csv` |

```bash
gcloud storage cp ensemble_outputs_clim_2025.csv \
  gs://${WEIGHTS_BUCKET}/blend/support/large/ensemble_outputs_clim_2025.csv

gcloud storage cp ensemble_outputs_clim_2025_mok.csv \
  gs://${WEIGHTS_BUCKET}/blend/support/large/ensemble_outputs_clim_2025_mok.csv
```

---

## Container Images

The pipeline uses six container images, all built from the `docker/` directory at the repo root.
Cloud Build builds them in parallel using `cloudbuild.yaml`.

| Image | Dockerfile | Target | Description |
|---|---|---|---|
| `monsoon-downloader` | `docker/downloader/Dockerfile` | Cloud Run Job | Downloads ECMWF and NCEP/GDAS initial conditions |
| `monsoon-postprocess` | `docker/postprocess/Dockerfile` | Cloud Run Job | Post-processes raw model output (CDO/NCL operations) |
| `monsoon-blend` | `docker/blend/Dockerfile` | Cloud Run Job | Blends model outputs, applies logistic regression, generates visualizations |
| `monsoon-sync` | `docker/sync/Dockerfile` | Cloud Run Job | Syncs final outputs to the operational web repository |
| `monsoon-aifs` | `docker/aifs/Dockerfile` | Cloud Batch | Runs AIFS GPU inference |
| `monsoon-neuralgcm` | `docker/neuralgcm/Dockerfile` | Cloud Batch | Runs NeuralGCM GPU inference |

### Build Context

All Dockerfiles use the **repository root** as the Docker build context. This allows Dockerfiles to
`COPY` from sibling directories (e.g., the `downloader` image copies `AIFS/utils/download_ic.py`
directly into the container). Do not change to the `docker/<name>/` directory before building —
always build from the repo root with `-f docker/<name>/Dockerfile .`.

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
Cloud Workflows (main pipeline)
    │
    ├── Cloud Run Job: downloader   (ECMWF + NCEP downloads, parallel)
    │
    ├── Cloud Batch Job: aifs       (GPU inference, parallel with neuralgcm)
    ├── Cloud Batch Job: neuralgcm  (GPU inference, parallel with aifs)
    │
    ├── Cloud Run Job: postprocess  (sequential)
    ├── Cloud Run Job: blend        (sequential)
    └── Cloud Run Job: sync         (sequential)
```

### Networking (`modules/networking`)

- **VPC** with a single regional subnet (`10.x.x.0/24`), private Google access enabled
- **Cloud NAT** for outbound internet access (downloading external forecast data) without public IPs
- **Firewall rules**: internal VPC traffic, IAP SSH (port 22 from `35.235.240.0/20`), and Google
  health check ranges
### Storage (`modules/storage`)

- **Main data bucket** (`monsoon-{env}-data-{project_id}`): stores raw downloads, intermediate
  files, and pipeline outputs per forecast region. Lifecycle rules delete `raw/` and
  `intermediate/` objects after `retention_days`; `output/` objects optionally transition to
  Nearline after `archive_after_days`.
- **Weights bucket** (`monsoon-{env}-weights-{project_id}`): stores model weights. Versioning is
  always enabled; `force_destroy = false` even in dev.
- **Artifact Registry** (`monsoon-{env}-containers`): Docker image registry for all pipeline
  images. Dev environments apply a 30-day cleanup policy.
- **Pipeline service account** with `storage.objectAdmin` on the data bucket,
  `storage.objectViewer` on the weights bucket, and `artifactregistry.reader` on the registry.
- **GCS folder structure** per forecast region: `{region}/config/`, `support/`, `output/`,
  `raw/`, `intermediate/`.

### Compute (`modules/compute`)

Creates Cloud Run v2 Jobs (not Services — these are batch workloads, not HTTP servers):

| Job | Memory | CPU | Timeout |
|---|---|---|---|
| downloader | 2 Gi | 2 | 15 min |
| postprocess | 16 Gi | 4 | 30 min |
| blend | 8 Gi | 4 | 30 min |
| sync | 1 Gi | 1 | 10 min |

All Cloud Run Jobs:
- Run under the pipeline service account
- Receive `ENVIRONMENT`, `GCS_BUCKET`, `GCS_WEIGHTS_BUCKET`, `PROJECT_ID`, and `FORECAST_REGION`
  as environment variables (region is overridden at execution time by the workflow)

GPU inference (AIFS and NeuralGCM) runs on **Cloud Batch**, not Cloud Run, because model
execution requires accelerators.

### Orchestration (`modules/orchestration`)

- **Cloud Workflows** (`monsoon-{env}-pipeline`): a YAML workflow that drives the full pipeline.
  See `modules/orchestration/workflow.yaml.tpl` for the full step definition.
  - Accepts `region` and optionally `date` as inputs
  - If no date is provided, runs the `downloader` in `get_latest_date` mode and reads the result
    from GCS (`{region}/intermediate/latest_date.txt`)
  - Downloads ECMWF and NCEP data in parallel
  - Runs AIFS and NeuralGCM as Cloud Batch jobs in parallel, polling every 60–120 seconds
  - Runs postprocess → blend → sync sequentially
- **Cloud Scheduler jobs** (one per `forecast_regions` entry): trigger the Workflow on
  `pipeline_schedule` (default every 6 hours in dev, configurable in prod)
- **Pub/Sub topics**: `pipeline-triggers`, `pipeline-completions`, and `dead-letter` for event
  routing (not yet wired into the workflow; reserved for future integrations)
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
environment's `main.tf` and pass them into the module. For model Batch jobs, use `gpu_type` and
`gpu_machine_type`.

### Updating Container Images Without Terraform

After a `gcloud builds submit`, new images are tagged `latest` and pushed to Artifact Registry.
Cloud Run Jobs pick up the new image on the next execution automatically — no `tf apply` needed.
The `lifecycle { ignore_changes }` block on each job ensures Terraform won't overwrite your latest
push on the next plan/apply.
