# Monsoon-Onset Operations Manual - Google Cloud Pipeline

**Audience:** anyone who has to run, monitor, debug, or change the cloud version of the
monsoon-onset forecast pipeline - **including people who have never used Google Cloud or Terraform
before**. No prior cloud experience is assumed. Where a concept is new, it is explained before it
is used.

**Scope:** the **GCP cloud pipeline** only. The HPC/cron version that runs on the DSI cluster is a
separate system - see `OPERATIONS_MANUAL_DSI.md` for that. The two pipelines do the same science
(AIFS + NeuralGCM + GenCast forecasts, blended into a probabilistic onset forecast) but the cloud
version runs every stage as a container on Google Cloud instead of as a Slurm job on a shared
GPU box.

**The two companion documents you will also want open:**
- `README_CLOUD.md` - the deep technical reference for the workflow, step by step.
- `terraform/README.md` - how the infrastructure is deployed and how to stage the large files.

This manual ties those together and adds the operational and "I am new here" material: what the
cloud pieces are, the rules for changing anything, how to read the logs, and what to do when a
forecast is missing.

> **The single most important rule, stated up front:**
> You do **not** click around in the Google Cloud Console to change how this pipeline works, and you
> do **not** run `tofu apply` or `gcloud builds submit` from your laptop. **Every change - code
> *and* infrastructure - goes through a pull request into `main`.** Merging to `main` is what makes
> changes real: it builds the containers (Cloud Build) and applies the Terraform (a CI apply),
> automatically. Developers have read + preview rights only; they cannot apply infrastructure,
> submit builds, or push images. The Console is for *looking* (logs, job status, bucket contents),
> not for *changing*. Sections 2, 3, and 9 explain why and how in full.

---

## 0. The 60-second mental model

```
   Cloud Scheduler  (a cron clock living in Google Cloud)
        |  fires on a schedule, sends "run region=india"
        v
   Cloud Workflows  (the orchestrator - a YAML script that decides what to run)
        |  asks: what is the latest data? what is already done?
        |
        +--> pipeline-state   (Cloud Run Service: answers "what still needs doing?")
        |
        +--> downloader       (Cloud Run Job: fetch ECMWF + NCEP inputs -> bucket)
        |
        +--> AIFS / AIFS-ENS  (Cloud Batch GPU jobs: run the models)
        +--> NeuralGCM        (Cloud Batch GPU job)
        +--> GenCast          (Cloud TPU job, launched by the tpu-dispatch Cloud Run Job)
        |
        +--> blend            (Cloud Batch CPU job: combine models -> onset forecast)
        |
        +--> sync             (Cloud Run Job: push final products to Drive / web repo)
        v
   Cloud Storage (GCS) buckets hold EVERYTHING in between: inputs, weights,
   raw forecasts, blended outputs. Containers talk to each other only through
   the buckets - never directly.
```

Three ideas carry the whole system:

1. **Everything is a container.** Each stage (download, AIFS, NeuralGCM, blend, sync, ...) is a
   Docker image. The same science code from the repo runs inside it. (Section 4.)
2. **Containers communicate only through GCS buckets.** A stage reads its inputs from a bucket and
   writes its outputs back to a bucket. Nothing is passed in memory between stages. (Section 5.)
3. **The workflow is event-driven and idempotent.** It checks "is this already done?" before
   running anything, and downstream stages wake up when an upstream stage drops a small marker file
   in the bucket. Re-running a finished date is safe - it just no-ops. (Section 6.)

---

## 1. Background: what Terraform is and why we use it

If you already know Terraform, skip to Section 2.

### 1.1 The problem Terraform solves

A cloud pipeline is made of many pieces: buckets, networks, service accounts, scheduled jobs, the
workflow, permission grants, and so on. You *could* create all of those by hand in the Google
Cloud Console (the web UI). The trouble is:

- Nobody can tell what the "correct" configuration is, because it only exists in the running cloud.
- A hand change made at 2am to fix an incident is invisible to everyone else and is lost the next
  time something is rebuilt.
- You cannot review a Console click in a pull request, and you cannot easily undo it.
- Standing up a second copy (dev vs prod) means repeating dozens of clicks and getting them all
  identical.

### 1.2 What Terraform actually is

**Terraform** (we use **OpenTofu**, an open-source drop-in for Terraform - the commands are the
same, the files are the same) is **infrastructure as code**. You describe the cloud resources you
want in text files (`.tf` files), and Terraform makes the real cloud match that description.

The core loop is three commands:

| Command | What it does | Changes the cloud? |
|---|---|---|
| `tofu plan` | Compares your `.tf` files to the live cloud and prints exactly what it *would* add, change, or destroy. | No - read-only preview. |
| `tofu apply` | Performs the changes from the plan after you type `yes`. | Yes. |
| `tofu output` | Prints values the config exposes (bucket names, workflow URL). | No. |

Terraform keeps a **state file** (stored in a GCS bucket - see `terraform/README.md`) that records
what it believes the cloud currently looks like. That state is how `plan` knows what is different.

> In this repo the deploy steps use the alias `tf` for `tofu` in some commands and `tofu`
> elsewhere - they are the same tool. `terraform/README.md` is the authoritative deploy runbook.

### 1.3 Why this matters operationally

Because the infrastructure is code:

- The `.tf` files in `terraform/` are the **single source of truth**. If you want to know how the
  pipeline is wired, you read the code, not the Console.
- Any change is a **diff in a pull request** that someone can review.
- Dev and prod are the same modules with different variables, so they cannot silently drift apart.

### 1.4 The layout (so you know where things live)

```
terraform/
  environments/
    dev/main.tf     <- dev settings (preemptible GPUs, 6-hourly schedule, verbose logging)
    prod/main.tf    <- prod settings
  modules/
    networking/     <- VPC, subnet, Cloud NAT, firewall rules
    storage/        <- GCS buckets, Artifact Registry, the pipeline service account
    compute/        <- Cloud Run Jobs/Service, Cloud Batch job templates
    orchestration/  <- Cloud Workflows, Cloud Scheduler, Pub/Sub, the workflow service account
    monitoring/     <- alerting (off in dev)
```

An **environment** (`dev`, `prod`) is a thin file that picks variable values and calls the shared
**modules**. A **module** is a reusable bundle of resources. You almost never edit a module to make
a routine change; you change a variable in the environment file.

---

## 2. The golden rule: no changes through the Cloud Console

**All infrastructure changes go through Terraform. All code changes go through a pull request into
`main`. The Console is read-only for operators.**

### 2.1 Why

- A Console change is invisible to Terraform. The next time anyone runs `tofu plan`, Terraform sees
  the live cloud no longer matches the code and will offer to **revert your change** on the next
  `apply`. Your fix silently disappears, often at the worst time.
- A Console change is unreviewed and unlogged in git. Nobody can see what you did or why.
- It breaks the guarantee that the `.tf` files describe reality. Once that guarantee is gone, every
  future change becomes risky guesswork.

### 2.2 What "read-only in the Console" means in practice

| You may use the Console to ... | You may NOT use the Console to ... |
|---|---|
| Read logs (Logging) | Create/edit/delete a bucket, job, service, workflow, scheduler, SA, IAM grant |
| Watch a workflow execution or Batch job progress | Change a job's memory/CPU, image, env vars, or schedule |
| Inspect bucket contents | Change a firewall rule, network, or permission |
| Check that a scheduled job exists | "Quickly fix" anything by clicking |

If you find yourself wanting to change a setting, the correct move is: **edit the `.tf` file, open a
PR, get it reviewed, and merge it - the change is applied automatically by CI on merge to `main`**
(Section 9). You do not run `tofu apply` yourself; developers do not have apply rights. For an
urgent incident where you must act faster than a PR allows, see Section 8.6 - but even then you
reconcile back into Terraform afterward.

### 2.3 The one nuance: container images update outside `tofu apply`

There is exactly one thing that changes the cloud without `tofu apply`, and it is intentional:
**new container images**. When CI builds and pushes a new image (Section 3), Cloud Run Jobs and
Batch jobs pick up the new `:latest` image on their next run. Terraform is told to **ignore image
changes** on those resources (`lifecycle { ignore_changes }`), so it will not fight the new image.
This is by design - it lets you ship code without re-running Terraform. It is *not* a license to
hand-edit anything else.

---

## 3. CI/CD: how code reaches the cloud (pull request -> Cloud Build -> containers)

Nothing runs in the cloud until it is a built container image. This section explains the only path
code takes to get there.

### 3.1 The rule

**No change reaches the cloud without a pull request merged into `main`.** This holds for both kinds
of change, each with its own merge-triggered pipeline:
- **Code** (science, wrappers, Dockerfiles) -> merging to `main` triggers the **container build**
  (this section).
- **Infrastructure** (`terraform/**`) -> merging to `main` triggers a **`tofu apply`** run in CI
  (Section 3.7 and Section 9.3).

If it is not on `main`, the cloud is not running it - and no developer applies or builds by hand.

### 3.2 What Cloud Build is

**Cloud Build** is Google Cloud's build service. It runs a sequence of steps (defined in
`cloudbuild.yaml` at the repo root) on Google's infrastructure: it builds the Docker images, pushes
them to **Artifact Registry** (the private image registry), and rolls the Cloud Run services/jobs
to the new image. You do not run builds on your laptop for production - Cloud Build does it.

### 3.3 The trigger: merge to `main`

A **Cloud Build trigger** watches the GitHub repository. When a pull request is **merged into
`main`**, the trigger fires and runs `cloudbuild.yaml`. This is the CI/CD pipeline:

```
   open PR  -->  review  -->  merge to main
                                   |
                                   v
                       Cloud Build trigger fires
                                   |
                                   v
                cloudbuild.yaml runs (build -> push -> deploy)
                                   |
                                   v
              new images in Artifact Registry; Cloud Run rolled
                                   |
                                   v
              next pipeline run uses the new container code
```

Because the trigger only fires on `main`, and `main` is protected (no direct pushes - see the repo
rules: "No branches merged to main without admin approval"), **a PR is the only way code changes
the cloud.**

> **The trigger is configured outside Terraform, on purpose, for security.** The
> Cloud-Build-to-GitHub connection and the trigger itself are set up directly in Cloud Build and are
> deliberately **not** managed as Terraform resources: the repo connection involves credentials and
> a trust relationship to the source repository that we do not want to express in, or expose
> through, the infrastructure code or its state. So a `tofu plan` will **not** show the trigger, and
> you will **not** find a `google_cloudbuild_trigger` resource in `terraform/`. What Terraform does
> manage is the supporting IAM: `terraform/environments/dev/main.tf` grants the Cloud Build service
> account `roles/run.developer` and `roles/iam.serviceAccountUser` so the build's deploy steps can
> roll Cloud Run services/jobs to the freshly pushed image. To inspect or change the trigger
> itself, use Cloud Build (Console or `gcloud builds triggers ...`), not Terraform - this is the one
> documented exception to "infrastructure lives in Terraform," and it is an exception by design.

### 3.4 What `cloudbuild.yaml` does, in order

1. **detect-targets** - runs `scripts/cloudbuild_detect_targets.py`. It diffs the merge against the
   previous commit and, using `.image-deps.yaml`, decides **which images actually need rebuilding**.
   Only changed images are rebuilt; untouched ones are skipped. Writes the list to
   `/workspace/targets`, which every later step greps as a gate.
2. **pull-cache-*** - pulls the existing `:latest` image for each target to warm the Docker layer
   cache (faster builds). Failure is fine on a first/cold build.
3. **build-*** - builds each target image. **Every image uses the repo root as build context**
   (`-f docker/<name>/Dockerfile .`) so a Dockerfile can `COPY` from sibling directories like
   `AIFS/utils/` or `blend/utils/`.
4. **push-*** - pushes the rebuilt images to Artifact Registry, tagged with both the short commit
   SHA and `latest`.
5. **deploy-*** - for the Cloud Run service/jobs (`pipeline-state`, `downloader`, `tpu-dispatch`,
   `sync`) it runs `gcloud run ... update --image=...:<sha>`. Cloud Run pins an image digest at
   revision-create time and does not auto-follow a moving `:latest` tag, so these need an explicit
   update. **Cloud Batch and TPU jobs resolve `:latest` at submit time**, so they do not need a
   deploy step - the next pipeline run picks up the new image automatically.

### 3.5 `.image-deps.yaml`: which source paths rebuild which image

This file maps source paths to the images they affect. For example, a change under `AIFS/utils/`
rebuilds `aifs-v2` and `aifs-ens-v2`; a change to `cloudbuild.yaml` or `.image-deps.yaml` rebuilds
**everything** (the `global:` list). When you change code, glance at this file to predict which
containers your PR will rebuild. If you add a new source dependency for an image, add it here or
your change may not trigger a rebuild.

### 3.6 Running a build by hand (maintainer/bootstrap only - not for developers)

This exists for one-time bootstrap or maintainer recovery, and requires `cloudbuild.builds.create`,
which **developers are deliberately not granted** - normal changes must go through the merge-to-main
trigger (Section 3.3). Do not use this to ship a routine change.

```bash
# From the repo root. Build only what changed vs HEAD~1:
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_REPO=monsoon-dev-containers,_ENV=dev \
  --project=<PROJECT_ID>

# Force-rebuild a specific image (or "all"):
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_TARGETS=aifs-v2,_REPO=monsoon-dev-containers,_ENV=dev \
  --project=<PROJECT_ID>
```

`_TARGETS` accepts `auto` (diff-based, the default), `all`, or a comma list of image names.

### 3.7 Terraform changes use the same merge-to-main gate (plan on PR, apply on merge)

Infrastructure changes follow the identical principle as code: **no one applies from a laptop;
`main` applies.** Two Cloud Build triggers (region `us-central1`), scoped to `terraform/**`, drive
it:

| Trigger | Fires on | Build config | What it runs |
|---|---|---|---|
| `monsoon-dev-tf-plan` | a PR targeting `main` that touches `terraform/**` | `terraform/cloudbuild.plan.yaml` | `tofu plan` (read-only, posted for review) |
| `monsoon-dev-tf-apply` | push to `main` (a merged PR) touching `terraform/**` | `terraform/cloudbuild.apply.yaml` | `tofu apply -auto-approve` |

Both builds run as a dedicated **`monsoon-dev-tf-apply` service account** - the *only* identity with
apply-level permissions. Sensitive variables are not in git; the build fetches the real tfvars from
Secret Manager (`monsoon-dev-tfvars`) before running. Like the image-build trigger, these triggers
and the apply SA are configured **outside Terraform on purpose** (Section 3.3's reasoning, plus you
don't want the apply to be able to delete its own trigger).

What this means for you day to day: you edit `.tf`, open a PR, and the **plan appears in the PR's
Cloud Build run** for review; on merge, CI applies it. You never type `tofu apply`. Developer IAM
(`roles/viewer` + `serviceusage.serviceUsageConsumer` + `workflows.invoker`, granted to the
`monsoon-cloud-devs` group) deliberately excludes apply, build-submit, and image-push. See Section 9
for the full workflow.

---

## 4. The containers: one per pipeline stage

The pipeline is built out of nine container images, all stored in a single Artifact Registry
repository (`monsoon-{env}-containers`). Each image is a stage. Here is what each one is and does.

| Image | Runs as | Purpose |
|---|---|---|
| `monsoon-downloader` | Cloud Run Job | Downloads ECMWF and NCEP/GDAS initial conditions plus the GenCast SST, uploads them to the common bucket. |
| `monsoon-pipeline-state` | Cloud Run **Service** | Answers "what is the latest data, and what still needs doing?" The workflow queries it. |
| `monsoon-aifs-v2` | Cloud Batch (GPU) | Runs AIFS v2 deterministic inference + post-processing. |
| `monsoon-aifs-ens-v2` | Cloud Batch (GPU) | Runs AIFS-ENS v2 ensemble inference + post-processing. |
| `monsoon-neuralgcm` | Cloud Batch (GPU) | Runs the 30-member NeuralGCM ensemble + post-processing + merge. |
| `monsoon-gencast` | Cloud TPU VM | Runs GenCast inference across a TPU slice. |
| `monsoon-tpu-dispatch` | Cloud Run Job | Creates and polls the GenCast TPU queued resource (launches/monitors the TPU job). |
| `monsoon-blend` | Cloud Batch (CPU) | Combines model outputs, applies the trained blend model, makes maps + the onset CSV. |
| `monsoon-sync` | Cloud Run Job | Pushes final products to Google Drive / the operational web repo, writes `latest.txt`. |

(There are `docker/aifs/` (v1) and `docker/postprocess/` directories in the tree, but the cloud
pipeline does **not** build or run them - only the v2 AIFS variants run in the cloud.)

Each image's recipe is `docker/<name>/Dockerfile`; its entrypoint is `docker/<name>/src/main.py` -
the **wrapper script** (Section 7).

---

## 5. The cloud components and their role

This section defines each Google Cloud service the pipeline uses, in plain terms, and says exactly
what it does here. Read it once; it makes the rest of the manual legible.

### 5.1 Cloud Build - builds the containers

Covered in Section 3. **Role here:** on merge to `main`, build the changed Docker images, push them
to Artifact Registry, and roll the Cloud Run services/jobs to the new image. It is the only way
code gets into the cloud.

### 5.2 Cloud Workflows - the orchestrator

**What it is:** a serverless service that runs a YAML "program" - a sequence of steps that can call
HTTP endpoints, call other Google APIs, branch, and run things in parallel. It is not a container;
it is a managed step-runner.

**Role here:** `terraform/modules/orchestration/workflow.yaml.tpl` is the whole pipeline brain. One
execution:
1. Reads its argument (`region`, optionally `date`).
2. Calls **pipeline-state** to learn the latest ICs and what is already done.
3. If nothing is left to do, exits. Otherwise submits the missing work.
4. Launches the downloader, then the model jobs (in parallel), then blend, then sync.
5. Advances downstream stages when upstream stages drop `intermediate/*_done` marker files (via
   Pub/Sub -> Eventarc, see 5.10).

It submits Batch/TPU jobs with **stable IDs** (e.g. `aifs-single-v2-{date}`) so re-submitting the
same date reuses the existing job instead of duplicating it. That is what makes the workflow
idempotent.

### 5.3 Cloud Run Jobs and Services - lightweight container compute

**What it is:** Cloud Run runs a container for you without you managing any servers.
- A **Cloud Run Job** runs a container to completion (it starts, does work, exits). Good for batch
  tasks like download and sync.
- A **Cloud Run Service** runs a container that stays up and answers HTTP requests. Good for
  pipeline-state, which the workflow queries.

**Role here:** `downloader`, `sync`, and `tpu-dispatch` are **Jobs**; `pipeline-state` is a
**Service**. They are cheap, CPU-only, and quick. Resource limits (from `terraform/README.md`):

| Resource | Type | Memory | CPU | Timeout |
|---|---|---|---|---|
| downloader | Job | 4 GiB | 2 | 900 s |
| sync | Job | 1 GiB | 1 | 600 s |
| tpu-dispatch | Job | 1 GiB | 1 | 86400 s |
| pipeline-state | Service | default | - | - |

### 5.4 Cloud Batch - heavy GPU/CPU compute

**What it is:** Cloud Batch provisions a VM (optionally with GPUs), runs your container on it as a
job, and tears the VM down when done. You do not manage the VM; you describe the machine type,
accelerators, and timeout, and Batch handles the lifecycle.

**Role here:** the expensive stages run on Batch:
- **AIFS v2** (deterministic): GPU VM, ~8 vCPU / 32 GiB, 30-min max.
- **AIFS-ENS v2** (ensemble): GPU VM, ~8 vCPU / 64 GiB, 2-hour max.
- **NeuralGCM**: GPU VM, 48 vCPU / 340 GiB, 4 A100 GPUs by default, 1-hour max (30 ensemble members
  run in parallel across the GPUs).
- **blend**: CPU VM (`e2-highmem-4`, 4 vCPU / 32 GiB), 60-min max.

In **dev**, GPU VMs use **SPOT** (preemptible, cheaper, can be reclaimed mid-run); in **prod** they
use **STANDARD**. The workflow submits Batch jobs with `skip_polling: true` and detects completion
from the output markers in the bucket rather than by watching the job.

### 5.5 Compute Engine - the VMs and the network underneath

**What it is:** Compute Engine is Google's raw VM service. You rarely touch it directly here, but it
is the substrate: Cloud Batch jobs and TPU VMs are Compute Engine VMs, and the **VPC network** is a
Compute Engine resource.

**Role here:** the `networking` module creates a dedicated **VPC** (`monsoon-{env}-vpc`) with:
- A single regional **subnet** with **private Google access** on, so containers reach GCS and other
  Google APIs over Google's internal network without a public IP.
- **Cloud NAT** on a Cloud Router, giving containers outbound internet access (the downloader needs
  to reach ECMWF and NCEP) **without** any container having a public IP.
- **Firewall rules**: internal traffic within the subnet, SSH only from Google's IAP range
  (`35.235.240.0/20`), and Google health-check ranges.

The practical upshot: containers have no public IP, can pull from the internet through NAT, and can
be SSH'd into only through IAP. You normally never log into these VMs.

### 5.6 Cloud Storage (GCS) - the data lake / the bus between stages

**What it is:** GCS stores **objects** (files) in **buckets**. An object path looks like
`gs://bucket-name/some/prefix/file.nc`. There are no real folders - the `/`-separated prefix is
just part of the name.

**Role here:** GCS is how every stage communicates. A stage downloads its inputs from a bucket and
uploads its outputs back. Two kinds of bucket exist per environment (full layout in
`README_CLOUD.md`):

- **Common bucket** `monsoon-{env}-common-{project_id}` - shared, not region-specific. Holds:
  - `weights/` - staged model checkpoints, sparse matrices, blend support files (Section 5.11).
  - `ic/` - downloaded initial conditions (ECMWF, NCEP GDAS, GenCast SST).
  - `full_field/` - raw full-field model forecasts.
  - `intermediate/` - the small marker files and latest-date pointers (`latest.txt`,
    `latest_ecmwf_date.txt`, `{model}_{region}_{date}_done`).
  - `jax-cache/` - persistent JAX compilation cache.
- **Regional output buckets** `monsoon-{env}-{region}-{project_id}` - one per forecast region. Holds
  only post-processed products and the final blend outputs under `output/`, plus `latest.txt` (the
  per-region "this date is fully done" signal).

Lifecycle rules delete `ic/`, `intermediate/`, and `jax-cache/` after `retention_days` (30 in dev);
`weights/` and `output/`/`full_field/` are retained.

### 5.7 Artifact Registry - the image store

**What it is:** a private Docker registry. **Role here:** holds all nine pipeline images
(`monsoon-{env}-containers`). Cloud Build pushes here; Cloud Run/Batch/TPU pull from here. Dev
deletes old image versions after 7 days (keeping the 3 newest per image).

### 5.8 TPUs - the GenCast accelerator

**What it is:** a **TPU** (Tensor Processing Unit) is Google's custom ML accelerator, an alternative
to GPUs. Large TPU jobs run on a **slice** (many TPU chips spread across several **TPU VM hosts**)
requested as a **queued resource** (you ask for capacity; Google fulfills it when available).

**Role here:** GenCast runs on a **TPU v5p-64** slice (topology `2x4x4` = 32 chips across 8
`ct5p-hightpu-4t` hosts). It is not on the GPU Batch path. The `tpu-dispatch` Cloud Run Job creates
one queued resource per date with a stable ID `gencast-{date}` (re-submits poll the existing one),
runs the GenCast container on every TPU host with JAX distributed init, and tears it down. Only JAX
process 0 writes outputs; the other 7 hosts participate in the distributed computation but skip
duplicate writes. Default TPU zone is `us-central1-a`; `gencast_tpu_zone = "us-east5-a"` moves
capacity east (Terraform then adds a matching subnet + NAT).

### 5.9 Cloud Storage FUSE - making a bucket look like a local folder

**What it is:** **FUSE** lets a GCS bucket be **mounted as if it were a local directory**. A program
writes to `/mnt/disks/common/...` and the bytes land in the bucket transparently.

**Role here:** some stages produce very large, many-file outputs (Zarr stores with thousands of
chunk files). Uploading those file-by-file after the run is slow and fills the container's local
disk. Instead, those stages mount the common bucket at `/mnt/disks/common` with FUSE and write the
Zarr **straight to the bucket** as it is produced:
- **NeuralGCM** mirrors its full-field Zarr through FUSE with asynchronous writes
  (`NEURALGCM_ZARR_MIRROR_WORKERS`).
- **AIFS-ENS v2** writes its full-field Zarr through the FUSE mount (it fails loudly if the mount at
  `/mnt/disks/common` is absent).
- **GenCast** writes its Zarr to `GENCAST_OUTPUT_DIR` under the FUSE mount and also points its JAX
  compilation cache at `jax-cache/gencast/v5p-64` in the bucket so later runs reuse compiled
  kernels.
- **blend** and diagnostics mount the bucket read-heavy with a bounded file cache.

The Terraform `batch_model_resources` knobs `mount_common_bucket` and `gcs_mount_options` control
whether and how a Batch stage gets the FUSE mount. FUSE behavior is opt-in via env vars, so the same
science code still runs unchanged on HPC where there is no bucket.

### 5.10 Pub/Sub + Eventarc - the wake-up mechanism

**What it is:** **Pub/Sub** is a message queue; **Eventarc** routes events to a target.

**Role here:** when a stage finishes it writes a tiny `intermediate/{model}_{region}_{date}_done`
object to the common bucket. GCS emits an object-finalize notification to the `pipeline-triggers`
Pub/Sub topic; Eventarc routes that to the workflow, which then re-checks state and submits the next
ready stage. This is why the pipeline is "event-driven": blend starts the moment the last model
marker appears, without polling.

### 5.11 IAM service accounts - the identities

**What it is:** a **service account (SA)** is a non-human identity that a job runs as. **IAM roles**
grant it permissions. **Role here**, two SAs (full role tables in `README_CLOUD.md`):
- **`monsoon-{env}-workflow`** - used by Cloud Workflows + Cloud Scheduler. Can invoke Cloud Run,
  submit Batch jobs, manage TPU queued resources, write logs, and read/write marker objects.
- **`monsoon-{env}-pipeline`** - used by every container at runtime. Can read/write the data buckets
  and pull images. This is the identity your container code authenticates as when it calls GCS - you
  do not handle keys; the SA is attached to the job.

---

## 6. How one pipeline run flows end to end

Read this alongside the "Execution Flow Diagram" in `README_CLOUD.md`.

1. **Cloud Scheduler fires** on its cron schedule (every 6 h in dev) and POSTs `{"region":
   "india"}` to the workflow's executions API. It deliberately omits `date` so the workflow finds
   the latest itself.
2. **init** - the workflow sets variables: `region`, `requested_date` (empty if not given),
   `action` (defaults to `run`), and the bucket names (substituted in by Terraform at deploy time).
3. **pipeline-state** - the workflow calls the pipeline-state service. It probes external sources
   for the latest 00z ECMWF/NCEP cycles, then checks the buckets for cached ICs, completed
   forecasts, blendable outputs, and pending sync. Its answer drives everything else. Disabled
   models (`disabled_models` per environment) are stripped out here.
4. **check_action** - if `action == "check"` (a manual dry run), exit now without running anything.
5. **run models (parallel)** - for each model whose forecast is missing: if its ICs are absent, run
   the **downloader** for that source; once ICs are present, submit the model job (AIFS / AIFS-ENS
   on Batch GPU, NeuralGCM on Batch GPU, GenCast on TPU via tpu-dispatch). Each model writes its
   raw forecast to `full_field/` in the common bucket, its post-processed products to
   `output/` in the regional bucket, and a `intermediate/{model}_{region}_{date}_done` marker.
6. **blend** - when the model markers it needs are present (the readiness gate imports
   `blend/utils/main.py`), the blend Batch job runs, writes `output/blend/{date}/` to the regional
   bucket, and drops `intermediate/blend_{region}_{date}_done`.
7. **sync** - the sync Cloud Run Job stages the configured rules from the regional bucket, pushes to
   Drive, and finally writes `latest.txt` to the regional bucket. **Writing `latest.txt` is the
   commit point**: it is the dedup signal pipeline-state reads next time. If the run failed earlier,
   `latest.txt` is not written, so the next scheduler tick retries the same date.
8. **log_complete / return_result** - the workflow returns the dates used or skipped per stage.

The key operational consequence: **a failed run leaves `latest.txt` un-advanced, so the next
scheduled tick automatically retries the unfinished work for the same date.** Often the fix for a
transient failure is simply to wait for the next tick or trigger one manually (Section 8.4).

---

## 7. Wrapper scripts: why they exist and how they make the code run in the cloud

This is the concept that ties the repo's science code to the cloud. Every container's entrypoint is
`docker/<name>/src/main.py` - the **wrapper script** (also called a "shim"). Understanding it is the
key to debugging the cloud pipeline.

### 7.1 The problem the wrapper solves

The science code (`AIFS/utils/run_model.py`, `NeuralGCM/utils/post_process.py`,
`blend/utils/main.py`, ...) was written to run on a normal filesystem: it reads inputs from
repo-relative paths like `AIFS/output/raw/...` and writes outputs to repo-relative paths. On HPC
that is exactly how it runs.

In the cloud there is **no shared filesystem**. Each container starts empty, runs on a throwaway VM,
and disappears. The inputs live in a GCS bucket and the outputs must end up in a GCS bucket. If you
tried to run `run_model.py` directly in a container, it would not find its inputs and its outputs
would vanish when the container exits.

### 7.2 What the wrapper does

The wrapper is a thin adapter that makes the cloud environment look like the filesystem the science
code expects. It does **not** reimplement the science - it calls the unchanged science script via
`subprocess`. The pattern, every time:

```
1. Read config from env vars   (DATE, MODEL, FORECAST_REGIONS, GCS_COMMON_BUCKET, GCS_REGION_BUCKETS)
2. Create the repo-shaped local directories the science code expects
3. DOWNLOAD inputs from the bucket into those local paths
       (initial conditions from ic/, weights from weights/)
4. RUN the unmodified science script with subprocess (cwd set to its utils/ dir, PYTHONPATH set)
5. UPLOAD the outputs from local paths back to the bucket
       (full-field -> common bucket, post-processed products -> regional bucket)
6. WRITE a tiny completion marker (intermediate/{model}_{region}_{date}_done)
```

The AIFS v2 wrapper (`docker/aifs-v2/src/main.py`) is the cleanest example - read it once and you
understand them all:

- It downloads the ECMWF GRIBs from `ic/ecmwf/{date}/grib/`, the checkpoint from
  `weights/aifs/<filename>` (filename read from `config/models.json`), and the two sparse `.npz`
  matrices from `weights/aifs/EKR/mir_16_linear/`.
- It runs the science with `subprocess.run([python, "run_model.py", "--date", date, "--model",
  model], cwd=AIFS/utils, ...)` - the *same* command an HPC operator would type.
- It runs `post_process.py` per region, uploads `output/{region}/...` to the regional bucket and the
  raw `init_{date}.nc` to `full_field/` in the common bucket.
- It writes `intermediate/{model}_{region}_{date}_done`, which wakes the workflow (Section 5.10).

### 7.3 Why this design is deliberate

- **The science code never changes for the cloud.** The same `run_model.py`, `post_process.py`,
  `blend/utils/main.py` run on HPC and in the cloud. Bugs are fixed once, in one place. The wrapper
  only handles "get inputs in, push outputs out."
- **Each container is self-contained and stateless.** It needs nothing from a previous container
  except what is in the bucket. That is what makes the workflow idempotent and re-runnable.
- **Authentication is automatic.** The wrapper uses `google.cloud.storage.Client()` with no keys -
  it authenticates as the pipeline service account attached to the job (Section 5.11).

### 7.4 Large files must live in the bucket, and the wrapper must reference them there

This is a hard rule and a common source of "it works on my machine but the container fails."

**GitHub is not a place for large or binary files.** Model checkpoints (`.ckpt`, `.pkl`), the sparse
interpolation matrices (`.npz`), blend support files (thresholds, climatology, shapefiles,
regridding weights), and any large data file are **gitignored** and are **not** in the container
image. If the science code needs such a file, it is **staged once into the common bucket** under the
`weights/` prefix (`terraform/README.md` -> "Staging Static Assets" has the exact upload commands),
and the **wrapper downloads it from the bucket at runtime** into the path the science code expects.

The contract, when you add or change a large file dependency:

1. Stage the file to the common bucket: `gs://{common_bucket}/weights/.../<file>` (one-time upload).
2. Make the wrapper download it to the **repo-relative local path** the science script reads
   (e.g. AIFS downloads to `AIFS/weights/` and `AIFS/EKR/mir_16_linear/`; NeuralGCM to
   `weights/neuralgcm/`; blend mirrors the whole `weights/blend/{region}/` tree into
   `/app/blend/data/`).
3. Keep the small, committed support files in the image as-is - only the large/gitignored ones are
   staged.

If you skip step 1, the container fails at download with a 404/`NotFound`. If you skip step 2, the
science script fails with `FileNotFoundError` even though the file is in the bucket. **A file being
in the bucket is not enough - the wrapper must fetch it to the exact local path the code reads.**
Conversely, do not try to `git add` a large file to make it available; it will be rejected or bloat
the repo, and the cloud will not use it anyway. The bucket is the home for large files.

---

## 8. Debugging playbook: a forecast is missing or a stage failed

Work top to bottom. Most incidents resolve by Step 3 or 4. The mental model: find which **stage**
broke, read **that stage's logs**, then either re-run that stage or wait for the next scheduler tick
to retry it.

### 8.1 Step 0 - Is it actually missing, or just not due yet?

The dev scheduler runs every 6 hours and ECMWF/NCEP publish hours after 00z. A gap a few hours after
00z is normal. Check whether the date should exist yet before assuming a failure.

### 8.2 Step 1 - What does the workflow say happened?

The workflow execution is the top-level record of a run. In the Console: **Workflows ->
`monsoon-{env}-pipeline` -> Executions tab**. Each execution shows the steps taken, the final
result JSON (which dates each stage used or skipped), and where it stopped or errored. In dev,
`LOG_ALL_CALLS` is on, so every step and variable value is in the execution history; in prod logging
is `LOG_NONE`, so you rely on the per-container logs below.

CLI equivalent:

```bash
gcloud workflows executions list monsoon-dev-pipeline \
  --location=us-central1 --project=<PROJECT_ID> --limit=5
gcloud workflows executions describe <EXECUTION_ID> \
  --workflow=monsoon-dev-pipeline --location=us-central1 --project=<PROJECT_ID>
```

The result JSON tells you which stage did not advance. Go to that stage's logs.

### 8.3 Step 2 - Read the failing stage's logs

All logs land in **Cloud Logging**. The fastest path is the Logs Explorer in the Console filtered by
resource type, or `gcloud logging read` from the CLI. Logs by component:

| Component | Resource type (Console filter) | What you see |
|---|---|---|
| Workflow steps + variables | `workflows.googleapis.com/Workflow` | step-by-step execution (dev only, `LOG_ALL_CALLS`) |
| Cloud Run Job/Service (downloader, sync, tpu-dispatch, pipeline-state) | `run.googleapis.com/CloudRunJob` (or the service) | container stdout/stderr |
| Cloud Batch (AIFS, AIFS-ENS, NeuralGCM, blend) | `batch.googleapis.com/Job` | container stdout/stderr from the VM |
| GenCast TPU VMs | log name `monsoon-tpu-vm` | per-host stdout/stderr; labels `worker_hostname`, `node_id`, `queued_resource_id`, `attempt`, `stream`. Full logs also uploaded under `intermediate/tpu-dispatch/.../logs/` |
| Cloud Build (the image build) | Cloud Build -> History | per-step build output |

CLI examples:

```bash
# Last 100 log lines from the blend Batch job (filter by your date if you can):
gcloud logging read \
  'resource.type="batch.googleapis.com/Job" AND labels.job_uid:"blend"' \
  --project=<PROJECT_ID> --limit=100 --freshness=1d

# Downloader (Cloud Run Job) logs:
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="monsoon-dev-downloader"' \
  --project=<PROJECT_ID> --limit=100 --freshness=1d

# Workflow execution logs:
gcloud logging read 'resource.type="workflows.googleapis.com/Workflow"' \
  --project=<PROJECT_ID> --limit=100 --freshness=1d
```

The wrappers log every GCS download/upload (`Downloaded gs://...`, `Uploaded ... -> gs://...`) and
the exact `subprocess` command they run, so the log shows precisely where a stage stopped - at a
missing input, in the science script, or at upload.

### 8.4 Step 3 - Confirm what is and is not in the buckets

Because stages talk only through GCS, "what exists in the bucket" is ground truth for how far a run
got. Check the markers and outputs for the date:

```bash
DATE=20260421T00
COMMON=monsoon-dev-common-<PROJECT_ID>
REGION_BUCKET=monsoon-dev-india-<PROJECT_ID>

# Initial conditions present?
gcloud storage ls gs://$COMMON/ic/ecmwf/$DATE/grib/
gcloud storage ls gs://$COMMON/ic/ncep/$DATE/

# Which model completion markers exist? (this tells you which models finished)
gcloud storage ls gs://$COMMON/intermediate/ | grep $DATE

# Regional outputs + the final commit signal:
gcloud storage ls gs://$REGION_BUCKET/output/blend/$DATE/
gcloud storage cat gs://$REGION_BUCKET/latest.txt
```

The first stage with no output/marker is where it broke. If `latest.txt` is behind the date you
expect, the run did not complete and will be retried.

### 8.5 Step 4 - Re-run

Three ways, least to most invasive:

1. **Let the scheduler retry.** Since `latest.txt` was not advanced, the next tick re-attempts the
   same date. For a transient SPOT preemption or a slow upstream, this is often all you need.
2. **Trigger the workflow now**, instead of waiting for the tick:

   ```bash
   # Latest date, normal run:
   gcloud workflows run monsoon-dev-pipeline \
     --location=us-central1 --project=<PROJECT_ID> \
     --data='{"region":"india"}'

   # Force a specific date:
   gcloud workflows run monsoon-dev-pipeline \
     --location=us-central1 --project=<PROJECT_ID> \
     --data='{"region":"india","date":"20260421T00"}'

   # Dry-run: see what pipeline-state thinks needs doing, run nothing:
   gcloud workflows run monsoon-dev-pipeline \
     --location=us-central1 --project=<PROJECT_ID> \
     --data='{"region":"india","action":"check"}'
   ```

   The workflow is idempotent: stages with outputs already present are skipped, so this only fills
   the gaps.
3. **Re-run one stage's container directly** when you want to isolate it. Cloud Run Jobs can be
   executed with env overrides:

   ```bash
   gcloud run jobs execute monsoon-dev-downloader \
     --region=us-central1 --project=<PROJECT_ID> \
     --update-env-vars=SOURCE=ecmwf,DATE=20260421T00,FORECAST_REGION=india
   ```

   For a model Batch job, the cleanest path is to re-trigger the workflow for the date (option 2);
   the workflow builds the correct Batch job spec for you.

### 8.6 Step 5 - If a stage produced partial/corrupt output that blocks a re-run

A half-written Zarr or truncated product can make a re-run skip (because "output exists") while the
output is actually bad. Delete just that object/prefix from the bucket, then re-run:

```bash
# Example: remove a bad NeuralGCM full-field Zarr for one date, then re-trigger the workflow.
gcloud storage rm -r gs://$COMMON/full_field/neuralgcm/$DATE.zarr/
```

> **Before deleting anything in a bucket:** confirm what it is and that you are in the right
> environment (dev vs prod project, right date). Initial conditions in `ic/` are shared by every
> model for that cycle - deleting them forces a re-download for all models. Prefer deleting the
> narrowest thing that unblocks the re-run. In prod, versioning is enabled, which gives some safety
> net; dev has no versioning.

### 8.7 Common failure signatures

| Symptom in logs | Likely cause | Action |
|---|---|---|
| `NotFound: ...weights/...` at download | large file not staged to the bucket | stage it (Section 7.4, `terraform/README.md`) |
| `FileNotFoundError` in the science script | wrapper did not download the file to the path the code reads | fix the wrapper's download path (Section 7.4) |
| Batch GPU job vanished/restarted mid-run (dev) | SPOT preemption | re-trigger; consider STANDARD if chronic |
| Workflow stuck after a model finished | the `*_done` marker was not written, so blend never woke | check that model's upload step finished; re-trigger |
| `sync` fails on Drive auth | OAuth creds/token missing or expired | check `GOOGLE_DRIVE_CREDENTIALS_JSON` / `GOOGLE_DRIVE_TOKEN_JSON` env/secret wiring |
| TPU job pending forever | TPU capacity unavailable in the zone | check tpu-dispatch logs; consider `gencast_tpu_zone` change (a Terraform/PR change) |
| New code not running in the cloud | image not rebuilt (path not in `.image-deps.yaml`) or PR not merged to main | check `.image-deps.yaml`; confirm the merge + Cloud Build ran |

---

## 9. Making changes safely: the development schema

This is the required process for changing anything. It exists so the cloud always matches the code
and every change is reviewable.

### 9.1 Decision: what kind of change is it?

| You want to change ... | That is a change to ... | Path |
|---|---|---|
| Science logic, a wrapper script, what a container does | **code** | PR -> merge to main -> Cloud Build rebuilds the image |
| Memory/CPU/GPU, schedule, retention, a new region, a new stage, IAM, network, env vars | **infrastructure** | edit `.tf` -> PR (CI posts `tofu plan`) -> review -> merge -> **CI applies** |
| A large data file the code needs | **staged asset** | `gcloud storage cp` to `weights/` + make the wrapper download it (Section 7.4) |

Most real changes touch more than one of these, and that is fine - they go in one PR. **In every
case the PR is the unit of change and the merge is what deploys.** No developer runs `tofu apply`,
`gcloud builds submit`, or `docker push` - those are CI's job and developers lack the rights to do
them (Section 3.7).

### 9.2 The workflow for a code change

1. Branch off `main`, make the change to the science code and/or `docker/<name>/src/main.py`.
2. If you changed which files an image depends on, update `.image-deps.yaml`.
3. Run `ruff check` and `ruff format` (the repo standard - see CLAUDE.md). Test locally where you
   can.
4. Open a PR. Get admin approval (the repo forbids merging to main without it).
5. Merge to `main`. Cloud Build fires, rebuilds the affected images, pushes them, and rolls Cloud
   Run. The next pipeline run uses the new code.
6. Verify: trigger a `check` run (Section 8.5) or watch the next execution's logs.

### 9.3 The workflow for an infrastructure change

**You never run `tofu apply`. CI applies on merge to `main`.** Your job is to write the change,
preview it locally, and open a PR; the pipeline (Section 3.7) does the rest.

1. Branch off `main`, edit the relevant `.tf` file. For routine changes that is an environment file
   (`terraform/environments/dev/main.tf`) or a module variable - not a module's internals.
2. Preview locally (optional but encouraged). Developer IAM is read-only on the state bucket, so
   pass `-lock=false`:

   ```bash
   cd terraform/environments/dev
   tofu init -backend-config="bucket=ai-weather-operational-tf-state"
   tofu plan -lock=false -var-file=terraform.tfvars
   ```

   Read the plan. It lists every add/change/destroy. **A `destroy` on a bucket or its data is a
   stop-and-think moment** - confirm it is intended. (If you lack a local `terraform.tfvars`, the
   real values live in Secret Manager `monsoon-dev-tfvars`; you can still review the CI plan instead
   of running one locally.)
3. Open a PR. The **`monsoon-dev-tf-plan`** trigger runs `tofu plan` and you (and reviewers) read
   the result in the PR's Cloud Build run - that is the authoritative plan. Get admin approval.
4. **Merge.** The **`monsoon-dev-tf-apply`** trigger runs `tofu apply -auto-approve` as the
   `monsoon-dev-tf-apply` service account. Watch it:

   ```bash
   gcloud builds list --region=us-central1 --project=ai-weather-operational --limit=5 \
     --format="table(id, status, substitutions.TRIGGER_NAME, createTime)"
   ```

5. Verify with `tofu output` (read-only) or by checking the resource in the Console (read-only).

Common infra changes have recipes in `terraform/README.md -> Extending the Infrastructure`: adding a
forecast region (add to `forecast_regions` - CI applies it on merge; the workflow is parameterized
by region), adding a pipeline stage, adding an environment, and changing Batch VM sizes via
`batch_model_resources`.

### 9.4 Dev first, then prod

`dev` and `prod` are separate environments (separate projects/buckets/schedules), each with its own
merge-triggered apply pipeline - so promoting a change to `prod` is still a PR/merge against the
prod environment's config, never a hand-run apply. Prove a change in `dev` (cheaper SPOT GPUs,
6-hourly schedule, verbose logging, force-destroyable buckets) before promoting it to `prod`. Key
differences: prod uses STANDARD GPUs, `LOG_NONE`, deletion protection on, versioning on, alerts on,
and no force-destroy on data buckets. For prod, add a manual-approval gate on the apply trigger so a
merge stages the apply and a human approves it after reading the plan (the dev apply trigger runs
unattended).

### 9.5 What never to do

- Do not change a resource in the Console (Section 2). It will be reverted or will cause drift.
- Do not run `tofu apply`, `gcloud builds submit`, or `docker push` by hand - merging to `main`
  applies and builds. Developers lack the rights to do these anyway (Section 3.7).
- Do not commit large/binary files to git (Section 7.4). Stage them in the bucket.
- Do not push to `main` without a reviewed PR (no direct pushes; admin approval required).
- Do not hand-edit a running Cloud Run/Batch job's settings; change the Terraform via a PR and let
  CI apply it on merge.

---

## 10. Quick reference card

```bash
PROJECT=<PROJECT_ID>; ENV=dev; LOC=us-central1
COMMON=monsoon-$ENV-common-$PROJECT
REGION_BUCKET=monsoon-$ENV-india-$PROJECT
DATE=20260421T00

# -- run / retry the pipeline ------------------------------------------------
gcloud workflows run monsoon-$ENV-pipeline --location=$LOC --project=$PROJECT \
  --data='{"region":"india"}'                                   # latest date
gcloud workflows run monsoon-$ENV-pipeline --location=$LOC --project=$PROJECT \
  --data='{"region":"india","date":"'$DATE'"}'                  # force a date
gcloud workflows run monsoon-$ENV-pipeline --location=$LOC --project=$PROJECT \
  --data='{"region":"india","action":"check"}'                  # dry run, runs nothing

# -- what happened -----------------------------------------------------------
gcloud workflows executions list monsoon-$ENV-pipeline --location=$LOC --project=$PROJECT --limit=5
gcloud logging read 'resource.type="batch.googleapis.com/Job"'  --project=$PROJECT --limit=80 --freshness=1d
gcloud logging read 'resource.type="cloud_run_job"'             --project=$PROJECT --limit=80 --freshness=1d

# -- what exists in the buckets (ground truth) -------------------------------
gcloud storage ls gs://$COMMON/ic/ecmwf/$DATE/grib/             # ECMWF ICs
gcloud storage ls gs://$COMMON/intermediate/ | grep $DATE       # model done-markers
gcloud storage ls gs://$REGION_BUCKET/output/blend/$DATE/       # blended outputs
gcloud storage cat gs://$REGION_BUCKET/latest.txt               # last completed date (commit signal)

# -- re-run a single Cloud Run Job by hand -----------------------------------
gcloud run jobs execute monsoon-$ENV-downloader --region=$LOC --project=$PROJECT \
  --update-env-vars=SOURCE=ecmwf,DATE=$DATE,FORECAST_REGION=india

# -- change infrastructure: edit .tf, then PR. CI applies on merge to main. --
#    You do NOT run tofu apply or gcloud builds submit - developers lack the rights.
cd terraform/environments/$ENV
tofu init -backend-config="bucket=${PROJECT}-tf-state"
tofu plan -lock=false -var-file=terraform.tfvars     # local read-only preview (optional)
tofu output                                          # read-only
#    Open a PR -> tf-plan build posts the plan -> merge -> tf-apply build applies.
gcloud builds list --region=$LOC --project=$PROJECT --limit=5 \
  --format="table(id, status, substitutions.TRIGGER_NAME, createTime)"   # watch the apply
```

---

## 11. Appendix

### 11.1 Service-to-cloud-component map

| Pipeline stage | Container image | Cloud service it runs on |
|---|---|---|
| download ICs | `monsoon-downloader` | Cloud Run Job |
| state check | `monsoon-pipeline-state` | Cloud Run Service |
| AIFS v2 det | `monsoon-aifs-v2` | Cloud Batch (GPU) |
| AIFS-ENS v2 | `monsoon-aifs-ens-v2` | Cloud Batch (GPU) |
| NeuralGCM | `monsoon-neuralgcm` | Cloud Batch (GPU) |
| GenCast | `monsoon-gencast` | Cloud TPU VM (via `monsoon-tpu-dispatch` Cloud Run Job) |
| blend + diagnostics | `monsoon-blend` | Cloud Batch (CPU) |
| sync to Drive/web | `monsoon-sync` | Cloud Run Job |
| orchestrate all of the above | (none - YAML) | Cloud Workflows + Cloud Scheduler |
| build the images (on merge to main) | `cloudbuild.yaml` | Cloud Build -> Artifact Registry |
| apply infrastructure (plan on PR, apply on merge) | `terraform/cloudbuild.{plan,apply}.yaml` | Cloud Build triggers `monsoon-dev-tf-plan` / `monsoon-dev-tf-apply` |

### 11.2 Where each kind of artifact lives in GCS

| Artifact | Bucket | Path |
|---|---|---|
| Staged weights / matrices / blend support | common | `weights/...` |
| Downloaded initial conditions | common | `ic/{ecmwf,ncep,gencast_sst}/{date}/...` |
| Raw full-field forecasts | common | `full_field/{model}/{date}/...` |
| Completion markers + latest-date pointers | common | `intermediate/...` |
| JAX compilation cache | common | `jax-cache/...` |
| Post-processed products | regional | `output/{model}/{date}/...` |
| Blended onset forecast + maps | regional | `output/blend/{date}/...` |
| Per-region commit signal | regional | `latest.txt` |

### 11.3 Logs at a glance

| Component | Cloud Logging resource type / log name |
|---|---|
| Workflow steps | `workflows.googleapis.com/Workflow` (dev: full history) |
| Cloud Run Jobs / Service | `cloud_run_job` / `cloud_run_revision` |
| Cloud Batch jobs | `batch.googleapis.com/Job` |
| GenCast TPU VMs | log name `monsoon-tpu-vm` (+ files under `intermediate/tpu-dispatch/.../logs/`) |
| Cloud Build | Cloud Build -> History |

### 11.4 Credentials / auth

You generally do not handle credentials: containers authenticate to GCS as the
`monsoon-{env}-pipeline` service account automatically. The exception is the **sync** job's Google
Drive OAuth, which is provided through the Terraform `external_api_secrets` map as
`GOOGLE_DRIVE_CREDENTIALS_JSON` / `GOOGLE_DRIVE_TOKEN_JSON`; the container writes them to
`/app/sync/.auth/` at runtime.

### 11.5 Notes for the maintainer

- This manual reflects the cloud pipeline as checked in (`cloudbuild.yaml`, `.image-deps.yaml`,
  `docker/*/src/main.py`, `terraform/`). If you change a wrapper's download paths, a container's
  responsibilities, a Batch resource size, or the workflow steps, update the relevant section here.
- **All deploy paths run on merge to `main`; no human applies or builds by hand.** There are three
  Cloud Build triggers, all configured in Cloud Build against the connected GitHub repo and
  **intentionally kept outside Terraform for security** (they carry repo connection credentials/trust
  and, for the apply trigger, would otherwise be able to delete themselves):
  - the image build trigger (`cloudbuild.yaml`) - rebuilds containers;
  - `monsoon-dev-tf-plan` (PR, `terraform/**`) - runs `tofu plan` for review;
  - `monsoon-dev-tf-apply` (push to `main`, `terraform/**`) - runs `tofu apply`.
  The tf triggers run as the dedicated **`monsoon-dev-tf-apply` service account** (the only identity
  with apply rights) and read sensitive vars from Secret Manager `monsoon-dev-tfvars`. Developer IAM
  (`roles/viewer` + `serviceusage.serviceUsageConsumer` + `workflows.invoker`, via the
  `monsoon-cloud-devs` group) excludes apply/build/push by design. Do not add a
  `google_cloudbuild_trigger` or the apply SA to `terraform/` without revisiting that decision.
  Manage all of these via Cloud Build / `gcloud` (`gcloud builds triggers ...`), not Terraform. The
  repo's Terraform only grants the *default* Cloud Build SA the Cloud Run deploy permissions
  (`terraform/environments/dev/main.tf`).
- Keep the staged-asset lists in `terraform/README.md` in sync with each wrapper's download logic -
  they are the authoritative "what large files must be in the bucket" reference.

---
*Document scope: GCP cloud pipeline. For the HPC/DSI cron pipeline see `OPERATIONS_MANUAL_DSI.md`.
For the deep technical workflow reference see `README_CLOUD.md`. For deployment and asset staging
see `terraform/README.md`.*
