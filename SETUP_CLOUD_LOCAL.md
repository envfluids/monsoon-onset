# Local Development Setup - Cloud Pipeline

**Audience:** a new developer joining the **cloud** monsoon-onset pipeline who needs to get their
**laptop** ready to read, change, and ship code. This guide assumes the **dev cloud environment
already exists** - someone has already run the initial Terraform deploy and staged the model
weights. You are joining an established project, not standing one up.

**What this guide is NOT:** it is not the first-time cloud bootstrap (creating the GCP project,
enabling APIs, creating the Terraform state bucket, the first `tofu apply`, staging weights). That
one-time work is in **`terraform/README.md`** and has already been done for `dev`. Do not repeat it.

**Read these first / alongside:**
- `OPERATIONS_MANUAL_CLOUD.md` - how the cloud pipeline works and the rules for changing it (the PR
  and Terraform workflow in its Section 9 is where you go after setup).
- `terraform/README.md` - the deploy and asset-staging reference (already executed for dev).
- `README_CLOUD.md` - the deep technical workflow reference.

> **The mental model for local dev:** your laptop is for **editing code, linting, running the light
> unit tests, inspecting the live dev cloud (read-only), and previewing Terraform changes
> (`tofu plan`)**. The heavy work - GPU/TPU model inference, full container runs - happens in the
> **cloud dev environment**, reached by merging a PR to `main` (which builds the containers via
> Cloud Build). You will not run AIFS or NeuralGCM on your laptop; you do not have the accelerators,
> the weights, or the inputs locally, and you do not need them.

---

## 0. Set your environment variables once

Every command below uses these. The dev GCP project is currently **`ai-weather-operational`**
(confirm with whoever onboarded you - treat it as configuration, not a constant). Put these in your
shell so the rest of the guide is copy-paste:

```bash
export PROJECT_ID=ai-weather-operational     # the existing dev GCP project
export REGION=us-central1                     # the dev region
export ENV=dev
export TF_STATE_BUCKET=${PROJECT_ID}-tf-state # Terraform remote-state bucket (convention from terraform/README.md)
```

If `${PROJECT_ID}-tf-state` is not the actual state bucket, ask a teammate or list candidates:
`gcloud storage ls --project=$PROJECT_ID | grep tf-state`.

---

## 1. Get access (do this before installing anything)

You cannot connect to the dev cloud until an admin grants you access. Ask for:

1. **Membership in the `monsoon-cloud-devs@uchicago.edu` group.** Developer access is granted to
   that Google Group, not to individuals - an admin adds you, and you inherit the developer role set
   automatically. The group holds exactly three project roles:
   - `roles/viewer` - read all resources (enables a clean `tofu plan`, plus logs/buckets/state),
   - `roles/serviceusage.serviceUsageConsumer` - use the project's APIs/quota,
   - `roles/workflows.invoker` - trigger test workflow runs.
   This is the **complete** developer set. It deliberately does **not** include the ability to
   `tofu apply`, submit a Cloud Build, push images, run jobs directly, or read secret values - those
   are CI's job or the operator tier (see OPERATIONS_MANUAL_CLOUD Sections 3.7 and 9). You do not
   need any of them to develop. (If you must `docker pull` images locally, ask for
   `roles/artifactregistry.reader` to be added to the group; `roles/viewer` does not cover image
   pulls.) External-domain collaborators who cannot be added to the `uchicago.edu` group get the
   same three roles bound directly to their user instead.
2. **GitHub access to this repository**, with permission to push branches and open PRs.
3. **GitHub access to the blend submodule repo.** The Ethiopia blend is a git submodule pointing at
   `git@github.com:amarchakitus/onsetblending2026.git` over SSH. If you do not have access to that
   repo (and an SSH key set up), the submodule checkout in Section 3 will fail.

You do **not** need a service-account key file. Local tools authenticate as **you** (your Google
account); the cloud jobs authenticate as their own service accounts. Never download or commit SA
keys.

---

## 2. Install the tools

| Tool | Why | Install |
|---|---|---|
| **Google Cloud SDK (`gcloud`)** | Talk to the dev project: logs, buckets, jobs, workflows. | https://cloud.google.com/sdk/docs/install |
| **OpenTofu** (or Terraform >= 1.5) | Preview infrastructure locally (`tofu plan`); applies happen only in CI on merge. Repo uses OpenTofu; commands are identical. | https://opentofu.org/docs/intro/install/ |
| **Docker** | Build container images locally when you change a wrapper or Dockerfile (optional - Cloud Build does the real builds). | https://docs.docker.com/get-docker/ |
| **Python 3.11** | Edit/lint/test the science and wrapper code. Matches the container base. | pyenv, conda, or your OS package manager |
| **git** (with SSH key on GitHub) | Clone the repo and the SSH submodule. | preinstalled on macOS/Linux |
| **ruff** | Required formatter/linter (must pass before pushing). | `pip install ruff` |

Verify:

```bash
gcloud version
tofu version          # or: terraform version
docker version        # optional
python --version      # expect 3.11.x
ruff --version
```

> This guide writes `tofu`. If you installed Terraform instead, substitute `terraform` - the
> arguments are the same.

---

## 3. Clone the repository (with submodules)

```bash
git clone --recurse-submodules git@github.com:<org>/monsoon-onset.git
cd monsoon-onset
```

If you already cloned without `--recurse-submodules`, initialize the submodule now:

```bash
git submodule update --init --recursive
```

A missing submodule shows up later as the Ethiopia blend failing on a missing module. If the
checkout fails with a permission error, you are missing access to the submodule repo (Section 1.3).

---

## 4. Authenticate to the existing dev cloud

Two separate logins, both needed:

```bash
# 1. Log in YOU for gcloud CLI commands (gcloud storage, gcloud logging, gcloud workflows, ...):
gcloud auth login

# 2. Set the active project and region:
gcloud config set project $PROJECT_ID
gcloud config set run/region $REGION

# 3. Application Default Credentials (ADC) - used by Terraform and by Google client libraries
#    (e.g. if you run a wrapper script locally). This is a SEPARATE credential from step 1:
gcloud auth application-default login
```

If you will build images locally, let Docker authenticate to the dev Artifact Registry:

```bash
gcloud auth configure-docker ${REGION}-docker.pkg.dev
```

Confirm you are pointed at the right place:

```bash
gcloud config list                 # account + project should be you + ai-weather-operational
gcloud projects describe $PROJECT_ID --format='value(projectId)'
```

---

## 5. Connect Terraform to the existing state (read-only first)

The dev infrastructure already exists and its Terraform **state lives in the GCS state bucket** -
you do not create anything. You point your local Terraform at that existing state so you can run
`tofu plan` and *see* the infrastructure and preview changes.

```bash
cd terraform/environments/dev

# Point at the existing remote state bucket. The prefix (monsoon/dev) is already set in main.tf.
tofu init -backend-config="bucket=${TF_STATE_BUCKET}"
```

Create a minimal `terraform.tfvars` (gitignored - never commit it) so the variables resolve:

```hcl
project_id = "ai-weather-operational"
region     = "us-central1"
```

Now preview. **`plan` is read-only and safe** - it changes nothing and is the right way to confirm
your setup and to see what the dev cloud currently is. Developer IAM is read-only on the state
bucket, so pass **`-lock=false`** (acquiring the normal state lock needs write, which you do not
have):

```bash
tofu plan -lock=false -var-file=terraform.tfvars
```

A healthy result on a correctly-deployed dev env is **"No changes. Your infrastructure matches the
configuration."** (or only the harmless `import` block reconciliation noted in `dev/main.tf`). See
the live resource names:

```bash
tofu output                        # common_bucket, region_buckets, workflow_url
```

> **You cannot - and never - run `tofu apply`.** Developers do not have apply permissions; applying
> is done **only by CI, automatically, when a PR merges into `main`** (the `monsoon-dev-tf-apply`
> Cloud Build trigger - see OPERATIONS_MANUAL_CLOUD Sections 3.7 and 9.3). Your local `tofu plan` is
> purely a preview; the authoritative plan is the one the `monsoon-dev-tf-plan` build posts on your
> PR. If `plan` shows unexpected changes you did not make, **stop** - that is drift or a stale local
> checkout; raise it, do not try to "fix" it. Some sensitive variables (`external_api_secrets`, the
> Drive OAuth vars) are intentionally not in the repo (they live in Secret Manager
> `monsoon-dev-tfvars`); you do not need them to `plan`, and you should not invent values for them.

---

## 6. Set up Python for editing, linting, and testing

You only need a light Python environment - enough to lint and run the unit tests and to import the
wrapper modules. You are **not** installing the full model stacks (PyTorch/JAX/etc.) locally.

```bash
# From the repo root, in a fresh virtualenv or conda env (Python 3.11):
python -m venv .venv && source .venv/bin/activate     # or use conda
pip install ruff pytest

# Install the deps for whichever component you are changing, from its container requirements file:
pip install -r docker/blend/requirements.txt          # example: working on the blend wrapper
```

The repo has **no single project-wide test runner or root `requirements.txt`** by design - each
container has its own `docker/<name>/requirements.txt`. Use focused checks (these match the repo's
contributor guidance in `AGENTS.md`):

```bash
# Syntax-check the entrypoints you touched:
python -m py_compile docker/blend/src/main.py docker/downloader/src/main.py

# Run the local unit tests (they stub out GCS, so they need no cloud access):
python -m pytest tests/ -q

# Lint + format (must pass before you push):
ruff check .
ruff format .

# Terraform formatting/validation if you edited any .tf:
tofu fmt -check -recursive ../../..    # from terraform/environments/dev; or run in terraform/
tofu validate
```

The `tests/` suite (`test_blend_wrapper.py`, `test_sync_wrapper.py`, `test_pipeline_state_actions.py`,
`test_tpu_dispatch.py`, `test_check_pipeline.py`, `test_workflow_template_contract.py`,
`test_drive_client.py`) loads the wrapper modules with a fake storage client, so it runs fully
offline. Run it before opening a PR.

---

## 7. Inspect the live dev pipeline (read-only)

With access set up, confirm you can see the running dev environment. This is also your everyday
"what is the cloud doing?" toolkit (full debugging playbook in OPERATIONS_MANUAL_CLOUD Section 8):

```bash
# Buckets: list the dev data buckets and peek at recent outputs:
gcloud storage ls --project=$PROJECT_ID | grep monsoon-$ENV
gcloud storage ls gs://monsoon-$ENV-common-$PROJECT_ID/intermediate/ | tail

# Workflow: list recent executions:
gcloud workflows executions list monsoon-$ENV-pipeline --location=$REGION --project=$PROJECT_ID --limit=5

# Logs: last lines from the blend Batch jobs:
gcloud logging read 'resource.type="batch.googleapis.com/Job"' --project=$PROJECT_ID --limit=20 --freshness=1d

# Scheduler + jobs exist?
gcloud scheduler jobs list --location=$REGION --project=$PROJECT_ID
gcloud run jobs list --region=$REGION --project=$PROJECT_ID
```

If these return data without permission errors, your access and auth are working.

---

## 8. Optional: build a container image locally

You rarely need this - merging to `main` builds images via Cloud Build (Section 3 of the ops
manual). But to debug a Dockerfile or wrapper build, build locally. **Always build from the repo
root** (the Dockerfiles `COPY` from sibling directories):

```bash
# From the repo root:
docker build -f docker/blend/Dockerfile -t monsoon-blend:local .
```

To build and push through Cloud Build instead (recommended over a manual `docker push`):

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_TARGETS=blend,_REPO=monsoon-$ENV-containers,_ENV=$ENV --project=$PROJECT_ID
```

Running a model container locally is generally not feasible (no GPU/TPU, no staged weights on your
machine). Validate model-code changes in the dev cloud via a PR + a workflow run, not on your
laptop.

---

## 9. Your setup is done - here is the daily loop

Once Sections 1-7 pass, you are set up. The development cycle from here is the one in
**OPERATIONS_MANUAL_CLOUD.md Section 9**, summarized:

1. Branch off `main`.
2. Edit code (science under `AIFS/`, `NeuralGCM/`, `blend/`, ... ; wrappers under
   `docker/<name>/src/main.py`) and/or infrastructure (`terraform/`).
3. If you changed which source paths an image depends on, update `.image-deps.yaml`.
4. Locally: `ruff check`, `ruff format`, `python -m pytest tests/`, `py_compile` touched
   entrypoints; for infra, `tofu plan -lock=false` to preview.
5. Open a PR. Include the affected stage, deployment impact, and the validation commands you ran.
   Get admin approval (required - no direct pushes to `main`). On the PR, CI runs the relevant
   checks: for `terraform/**` changes the `monsoon-dev-tf-plan` build posts the authoritative plan
   for review.
6. **Merge to `main` - the merge is what deploys.** Cloud Build rebuilds the changed images and
   rolls Cloud Run; for `terraform/**` changes, the `monsoon-dev-tf-apply` build runs
   `tofu apply` automatically. **You never run `tofu apply`, `gcloud builds submit`, or `docker
   push` yourself - you do not have those rights, and the merge does the work.**
7. Verify in the dev cloud: watch the build (`gcloud builds list --region=$REGION ...`), then
   trigger a `check` or full workflow run and watch the logs.

**Reminders that matter from day one:**
- Never change cloud resources by clicking in the Console - edit Terraform, open a PR, and let CI
  apply on merge (ops manual Section 2).
- Never run `tofu apply`, `gcloud builds submit`, or `docker push` by hand - the merge to `main`
  does it, and you do not have the rights anyway (ops manual Section 3.7).
- Never commit large/binary files (weights, `.npz`, shapefiles, model output) - they live in the
  GCS bucket and wrappers download them at runtime (ops manual Section 7.4).
- Never commit credentials, SA keys, `terraform.tfvars`, or local `.terraform/` state.

---

## 10. Troubleshooting setup

| Symptom | Cause | Fix |
|---|---|---|
| `tofu init` fails on the backend | wrong/missing state bucket name | set `TF_STATE_BUCKET` to the real bucket (Section 0); confirm you have `storage.objectViewer` on it |
| `tofu plan` shows resources being **created** | not pointed at the existing state, or stale checkout | re-run `tofu init -backend-config=...`; `git pull`; do not apply - ask |
| `tofu plan` errors on a missing variable | sensitive vars not set locally | you do not need them to plan; do not invent values - confirm with a teammate |
| `tofu plan` fails to acquire a state lock / `403` writing the lock | dev IAM is read-only on the state bucket | run `tofu plan -lock=false` (preview only; you are not meant to apply locally) |
| "How do I deploy my Terraform change?" | you can't apply by hand - that's intentional | merge the PR to `main`; the `monsoon-dev-tf-apply` build applies it automatically |
| `Permission denied` on `gcloud storage`/`logging` | missing IAM / not in the dev group | confirm you are in `monsoon-cloud-devs` (Section 1.1) |
| `gcloud auth` works but client libraries fail | ADC not set | run `gcloud auth application-default login` (separate from `gcloud auth login`) |
| submodule clone `Permission denied (publickey)` | no access/SSH key for the blend submodule repo | add your SSH key to GitHub; request access to `onsetblending2026` |
| `docker build` cannot find a `COPY` source | built from `docker/<name>/` instead of repo root | build from the repo root with `-f docker/<name>/Dockerfile .` |
| `docker push` to Artifact Registry unauthorized | Docker not configured for the registry | `gcloud auth configure-docker ${REGION}-docker.pkg.dev` |

---

## 11. Quick reference

```bash
# -- one-time per machine ----------------------------------------------------
export PROJECT_ID=ai-weather-operational REGION=us-central1 ENV=dev
export TF_STATE_BUCKET=${PROJECT_ID}-tf-state
gcloud auth login
gcloud config set project $PROJECT_ID
gcloud auth application-default login
gcloud auth configure-docker ${REGION}-docker.pkg.dev      # only if building images
git clone --recurse-submodules <repo-url> && cd monsoon-onset

# -- connect Terraform to existing dev (read-only) ---------------------------
cd terraform/environments/dev
tofu init -backend-config="bucket=${TF_STATE_BUCKET}"
printf 'project_id = "%s"\nregion = "%s"\n' "$PROJECT_ID" "$REGION" > terraform.tfvars
tofu plan -lock=false -var-file=terraform.tfvars   # read-only preview; expect "No changes"
tofu output
# NOTE: you never run `tofu apply` - merging a PR to main applies via CI.

# -- local dev checks (from repo root) ---------------------------------------
python -m venv .venv && source .venv/bin/activate && pip install ruff pytest
ruff check . && ruff format .
python -m pytest tests/ -q
python -m py_compile docker/<name>/src/main.py

# -- look at the live dev cloud ----------------------------------------------
gcloud storage ls --project=$PROJECT_ID | grep monsoon-$ENV
gcloud workflows executions list monsoon-$ENV-pipeline --location=$REGION --project=$PROJECT_ID --limit=5
gcloud logging read 'resource.type="batch.googleapis.com/Job"' --project=$PROJECT_ID --limit=20 --freshness=1d
```

---
*Document scope: getting a local dev environment ready against an already-deployed cloud dev
environment. To bootstrap a brand-new cloud environment, see `terraform/README.md`. To understand
and operate the running pipeline, see `OPERATIONS_MANUAL_CLOUD.md`.*
