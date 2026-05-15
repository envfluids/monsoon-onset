# Multi-Region Cloud Refactor + AIFS-ENS Support

## Context

The cloud setup currently treats each forecast region as if it owned its own copy of everything: ICs, weights references, and outputs all live under `gs://monsoon-{env}-data-{pid}/{region}/...`. The downloader is invoked per region and writes the same ECMWF/NCEP IC into every region's `raw/` prefix. The scheduler is per-region and the workflow takes a single `region` argument. Adding ethiopia today would mean duplicating ICs and re-running models per region.

The science code is already further along than the cloud setup: `AIFS/utils/post_process.py` accepts `--model AIFS|AIFS_ENS`, has `post_process_india` and `post_process_ethiopia` functions, and `AIFS/utils/run_model_ENS.py` exists alongside `run_model.py`. What is missing is a cloud layout that (a) shares ICs, weights, and full-field model outputs across regions/models, (b) only fans out to per-region buckets at the post-processed-output stage, and (c) lets the AIFS Docker image run either the deterministic or the ensemble model.

## Locked-in decisions

- One common bucket for shared files (weights, ICs, full-field outputs, intermediate markers). One bucket per region for region-specific outputs only.
- AIFS and AIFS-ENS share one Docker image (`docker/aifs/`); selection is by `MODEL` env var.
- No legacy buckets; everything fresh.
- Existing per-region output filenames are preserved (`tp_2p0_{date}.nc`, `tp_0p25_{date}.nc`, `tp_{date}.nc`, etc.); only the AIFS↔blend filename mismatch is fixed.
- All region-specific config lives in one terraform `regions` map. Cloud shims, workflow, terraform module logic never reference region names directly.
- Ethiopia bucket layout has an extra `/AIFS/` or `/AIFS_ENS/` level (accepted; no `post_process_ethiopia` refactor in scope).
- Ethiopia publishing is defined now (existing `sync.yaml` rules wired through cloud sync).
- NeuralGCM full-field upload is gated behind `upload_neuralgcm_full_field` feature flag; default `false`. AIFS full-field upload is unconditional.

## Region → model matrix

- `india`: AIFS deterministic + NeuralGCM, with India blend.
- `ethiopia`: AIFS deterministic + AIFS-ENS, no blend.

## Single source of truth: terraform `regions` variable

```hcl
# terraform/variables.tf
variable "regions" {
  type = map(object({
    models = list(string)              # which models produce output for this region
    stages = list(string)              # which post-model stages run: "blend", "sync"
    sync = object({
      rules     = list(string)         # sync.yaml rule names to invoke
      sources   = list(object({        # GCS → local staging mapping
        gcs_prefix    = string         # may contain {date} or {aifs_date}
        local_dir     = string         # may contain {date} or {aifs_date}
        date_kind     = string         # "date" or "aifs_date"
      }))
      git_push  = bool                 # push to monsoon-operational repo
      date_kind = string               # "date" (NeuralGCM) or "aifs_date" (AIFS-only)
    })
  }))
}

variable "upload_neuralgcm_full_field" {
  type    = bool
  default = false
}
```

```hcl
# environments/dev/main.tf and environments/prod/main.tf
regions = {
  india = {
    models = ["aifs", "neuralgcm"]
    stages = ["blend", "sync"]
    sync = {
      rules = ["blend_google"]
      sources = [
        { gcs_prefix = "output/blend/{date}/", local_dir = "blend/output_google/india/{date}/", date_kind = "date" },
      ]
      git_push  = true
      date_kind = "date"
    }
  }
  ethiopia = {
    models = ["aifs", "aifs_ens"]
    stages = ["sync"]
    sync = {
      rules = ["AIFS", "AIFS_ENS"]
      sources = [
        { gcs_prefix = "output/aifs/{aifs_date}/AIFS/",         local_dir = "AIFS/output/ethiopia/AIFS/",     date_kind = "aifs_date" },
        { gcs_prefix = "output/aifs_ens/{aifs_date}/AIFS_ENS/", local_dir = "AIFS/output/ethiopia/AIFS_ENS/", date_kind = "aifs_date" },
      ]
      git_push  = false
      date_kind = "aifs_date"
    }
  }
}
```

Adding a region = one map entry; no other code changes.

## Target GCS layout

**Common bucket** — `monsoon-{env}-common-{project_id}`

```
weights/
  aifs/aifs-single-mse-1.1.ckpt
  aifs/aifs-ens-crps-1.0.ckpt                 # NEW (upload manually after apply)
  aifs/EKR/mir_16_linear/{sparse-hash}.npz
  neuralgcm/models_v1_precip_stochastic_precip_2_8_deg.pkl
  neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc
  blend/india/support/large/...               # india blend supports
ic/
  ecmwf/{date}/input_state_{date}.pkl         # shared by AIFS + AIFS-ENS, all regions
  ncep/{date}/gdas_{date}.pgrb2               # shared by NeuralGCM, all regions
full_field/
  aifs/{date}/init_{date}.nc                  # raw AIFS deterministic output
  aifs_ens/{date}/init_{date}.zarr/           # raw AIFS-ENS output
  neuralgcm/{date}/...                        # gated on upload_neuralgcm_full_field
intermediate/
  latest_date.txt
  latest_ecmwf_date.txt
  latest_ncep_date.txt
  {model}_{region}_{date}_done                # per-(model,region) completion marker
```

**Region buckets** — `monsoon-{env}-region-{region}-{project_id}`

```
output/
  aifs/{date}/<post_process layout for that region>
  aifs_ens/{date}/<post_process layout for that region>
  neuralgcm/{date}/<post_process layout for that region>
  blend/{date}/...                            # only regions whose stages include "blend"
latest.txt                                    # written by sync
```

## Cloud-layer principles (no region hardcoding)

- Cloud shims, workflow, terraform module logic never contain region names or `if region == "..."` branches.
- Shims receive `FORECAST_REGIONS` (JSON list) + `GCS_REGION_BUCKETS` (JSON map) + per-region specs (JSON) and iterate.
- Per-region completion markers replace per-region file-list verification: verifier checks markers only.
- `post_process.py` is the single place region names appear in code (small dispatch table, science layer).

## `AIFS/utils/post_process.py` changes

Add `--region` arg with a dispatch table; normalize handler signatures.

```python
REGION_HANDLERS = {
    "india":    post_process_india,
    "ethiopia": post_process_ethiopia,
}

def main():
    parser.add_argument("--region")          # optional; if omitted, runs all (HPC compat)
    parser.add_argument("--model", default="AIFS")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    ds = _load(args.model, args.date)
    targets = [args.region] if args.region else list(REGION_HANDLERS)
    for r in targets:
        REGION_HANDLERS[r](ds, args.date, args.model)
```

`post_process_india` signature normalized to `(ds, date, model)` (model arg ignored). HPC scripts that omit `--region` keep current behavior.

## Docker shim changes

### `docker/downloader/src/main.py`
- Drop `FORECAST_REGION` from IC paths.
- Write ECMWF to `gs://common/ic/ecmwf/{date}/input_state_{date}.pkl`.
- Write NCEP to `gs://common/ic/ncep/{date}/gdas_{date}.pgrb2`.
- Markers (`latest_date.txt`, `latest_ecmwf_date.txt`, `latest_ncep_date.txt`) move to `gs://common/intermediate/`.
- Sparse-matrix probe still reads `gs://common/weights/aifs/EKR/mir_16_linear/...`.

### `docker/aifs/src/main.py` (deterministic + ensemble)
Env: `MODEL` (`aifs`|`aifs_ens`), `DATE`, `GCS_COMMON_BUCKET`, `GCS_REGION_BUCKETS`, `FORECAST_REGIONS`.

Behavior:
1. `aifs_date = DATE - 12h`. Read `latest_ecmwf_date.txt` from `gs://common/intermediate/`.
2. Download IC from `gs://common/ic/ecmwf/{ecmwf_date}/input_state_{ecmwf_date}.pkl`.
3. Download weights from `gs://common/weights/aifs/...` (deterministic ckpt for `aifs`, ENS ckpt for `aifs_ens`, plus shared sparse transform).
4. Run model:
   - `aifs` → `python run_model.py --date {aifs_date}` → `../raw/output/AIFS/init_{aifs_date}.nc`
   - `aifs_ens` → `python run_model_ENS.py --date {aifs_date}` → `../raw/output/AIFS_ENS/init_{aifs_date}.zarr`
5. Upload full-field to `gs://common/full_field/{aifs|aifs_ens}/{aifs_date}/`.
6. For each `region` in `FORECAST_REGIONS`:
   - Run `python post_process.py --date {aifs_date} --model {AIFS|AIFS_ENS} --region {region}`.
   - Upload `../output/{region}/` recursively to `gs://region-{region}/output/{model}/{aifs_date}/`.
   - Write completion marker `gs://common/intermediate/{model}_{region}_{aifs_date}_done`.

The shim has zero region-specific control flow.

### `docker/neuralgcm/src/main.py`
Env: `DATE`, `GCS_COMMON_BUCKET`, `GCS_REGION_BUCKETS`, `FORECAST_REGIONS`, `UPLOAD_FULL_FIELD`.

Behavior:
1. Download IC from `gs://common/ic/ncep/{date}/gdas_{date}.pgrb2`.
2. Download weights + forcings from `gs://common/weights/neuralgcm/...`.
3. `preprocess.py → run_model.py`.
4. If `UPLOAD_FULL_FIELD=true`: upload `../raw/output/{date}/` to `gs://common/full_field/neuralgcm/{date}/`. (Gated by feature flag, default off.)
5. `post_process.py → post_process_merge.py`.
6. For each `region` in `FORECAST_REGIONS` (today: `["india"]`):
   - Upload `../output/` to `gs://region-{region}/output/neuralgcm/{date}/`.
   - Write completion marker `gs://common/intermediate/neuralgcm_{region}_{date}_done`.

(NeuralGCM science currently produces a single India-shaped output. When a future region needs NeuralGCM, add a region dispatch in `NeuralGCM/utils/post_process_merge.py` analogous to AIFS.)

### `docker/postprocess/src/main.py` (verifier gate)
Env: `FORECAST_REGION`, `DATE`, `GCS_COMMON_BUCKET`, `REGION_MODELS` (JSON map of region → models).

Behavior: for each model in `REGION_MODELS[FORECAST_REGION]`, require `gs://common/intermediate/{model}_{FORECAST_REGION}_{date_for_model}_done`. Date convention: `aifs*` use `aifs_date = date - 12h`; `neuralgcm` uses `date`. Verifier never enumerates output files; markers are the contract.

### `docker/blend/src/main.py` (regions whose stages include `blend`)
- Read `GCS_COMMON_BUCKET`, `GCS_REGION_BUCKETS`.
- Download AIFS TP from `gs://region-{FORECAST_REGION}/output/aifs/{aifs_date}/tp/tp_2p0_{aifs_date}.nc` to local `/app/AIFS/output/tp/tp_{aifs_date}.nc` (renames to fix the existing AIFS↔blend filename mismatch without touching `blend/utils/india2025/main.py`).
- Download NeuralGCM TP from `gs://region-{FORECAST_REGION}/output/neuralgcm/{date}/tp/tp_{date}.nc`.
- Download blend supports from `gs://common/weights/blend/{FORECAST_REGION}/support/large/`.
- Upload blend output to `gs://region-{FORECAST_REGION}/output/blend/{date}/`.

(Today only india has `blend` in its stages. The shim is region-parameterized, so adding a region with blend = add `blend/{region}/support/` to common + add `"blend"` to that region's `stages`.)

### `docker/sync/src/main.py`
Env: `FORECAST_REGION`, `DATE`, `GCS_REGION_BUCKETS`, `SYNC_SPEC` (JSON: the region's `sync` block from terraform), `ENABLE_DRIVE`.

Behavior:
```python
spec = json.loads(os.environ["SYNC_SPEC"])
date = os.environ["DATE"]
aifs_date = shift_12h(date)
region_bucket = json.loads(os.environ["GCS_REGION_BUCKETS"])[FORECAST_REGION]

with tempfile.TemporaryDirectory() as tmp:
    sync_root = Path(tmp) / "sync-root"
    for src in spec["sources"]:
        d = aifs_date if src["date_kind"] == "aifs_date" else date
        gcs_prefix = src["gcs_prefix"].format(date=date, aifs_date=aifs_date)
        local_dir  = sync_root / src["local_dir"].format(date=d, aifs_date=aifs_date)
        download_gcs_prefix(region_bucket, gcs_prefix, local_dir)

    if enable_drive:
        run_sync_engine(sync_root, FORECAST_REGION,
                        rule_names=set(spec["rules"]),
                        dates={aifs_date if spec["date_kind"]=="aifs_date" else date})

    if spec["git_push"]:
        push_to_operational_repo(...)        # india only

write_gcs_text(region_bucket, "latest.txt",
               aifs_date if spec["date_kind"]=="aifs_date" else date)
```

Container has zero region names; ethiopia publishing works via existing `sync/config/sync.yaml` ethiopia rules.

### `docker/pipeline-state/src/main.py`
Env: `GCS_COMMON_BUCKET`, `GCS_REGION_BUCKETS`, `REGIONS` (full map JSON-encoded).

Inspects:
- IC: `gs://common/ic/{ecmwf,ncep}/{date}/...`
- Per-(model, region) completion markers: `gs://common/intermediate/{model}_{region}_{date}_done`
- Per-region blend (where applicable): `gs://region-{region}/output/blend/{date}/`
- Per-region latest: `gs://region-{region}/latest.txt`

HTTP response shape:
```json
{
  "date": "...",
  "ic": {"ecmwf": {"date": "...", "present": true}, "ncep": {...}},
  "models": {
    "aifs":     {"complete": true, "regions": {"india": {"present": true}, "ethiopia": {"present": true}}},
    "aifs_ens": {"complete": true, "regions": {"ethiopia": {"present": true}}},
    "neuralgcm":{"complete": true, "regions": {"india": {"present": true}}}
  },
  "per_region": {
    "india":    {"blend": {"present": true},  "sync": {"present": true, "latest": "..."}},
    "ethiopia": {"sync": {"present": true, "latest": "..."}}
  }
}
```

The workflow consumes this shape to decide which models and which regions still need work.

## Terraform changes

### `terraform/modules/storage/`
- Delete `google_storage_bucket "main"` and `google_storage_bucket "weights"`.
- Add `google_storage_bucket "common"` named `${name_prefix}-${environment}-common-${project_id}`.
- Add `google_storage_bucket "region"` with `for_each = var.regions` (keys), named `${name_prefix}-${environment}-region-${each.key}-${project_id}`.
- Replace `google_storage_bucket_object.region_folders` with two marker sets:
  - On `common`: `weights/.keep`, `ic/.keep`, `full_field/.keep`, `intermediate/.keep`.
  - On each `region` bucket: `output/.keep`.
- Lifecycle on `common`: rules apply to `ic/` and `intermediate/` prefixes (delete after `retention_days`); keep `weights/` and `full_field/` (long archive window via existing `archive_after_days`).
- Lifecycle on region buckets: existing `output/` rules.
- IAM on the pipeline SA: `roles/storage.objectAdmin` on `common` and on every region bucket.
- Outputs:
  - `common_bucket_name` (string)
  - `region_bucket_names` (`map(string)`, keyed by region)
  - Drop `bucket_name`, `weights_bucket_name`, `bucket_url`, `weights_bucket_url`.

### `terraform/modules/compute/`
- Replace `GCS_BUCKET` / `GCS_WEIGHTS_BUCKET` env vars on every job with:
  - `GCS_COMMON_BUCKET = var.common_bucket`
  - `GCS_REGION_BUCKETS = jsonencode(var.region_buckets)` — JSON map `{region: bucket}`
- Drop hardcoded default `FORECAST_REGION = "india"`. Workflow supplies `FORECAST_REGION` at runtime per region for postprocess/blend/sync. Downloader and model jobs do not set `FORECAST_REGION` (they operate against `common`).
- Add `REGION_MODELS = jsonencode({for k,v in var.regions : k => v.models})` to postprocess and pipeline-state jobs.
- Add `REGIONS = jsonencode(var.regions)` to pipeline-state.
- Add `UPLOAD_FULL_FIELD = tostring(var.upload_neuralgcm_full_field)` to NeuralGCM job.
- Add `SYNC_SPEC` (per-region JSON-encoded `regions[region].sync`) to sync job, supplied at runtime by the workflow.
- `aifs_image` is used for both AIFS and AIFS-ENS jobs (same Docker image).

### `terraform/modules/orchestration/`
- Drop `for_each = toset(var.forecast_regions)` on `google_cloud_scheduler_job.pipeline_trigger`. **One scheduler** triggers the workflow with no per-region argument.
- Update workflow IAM: grant `storage.objectAdmin` on `var.common_bucket` and on every entry of `var.region_buckets`.
- Pass `common_bucket`, `region_buckets`, `regions` (full map) into `workflow.yaml.tpl`.

### `terraform/modules/orchestration/workflow.yaml.tpl`
Region-agnostic backbone with data-driven fan-out:

1. **init** — substitute `regions` map into the workflow. Compute `regions_by_model` (inverse).
2. **probe state** — call `pipeline-state` once.
3. **download ICs** — fan out by IC source. ECMWF if any region uses `aifs` or `aifs_ens`; NCEP if any uses `neuralgcm`.
4. **run models** — for each `model` in `keys(regions_by_model)`, parallel branch with `MODEL=<model>` and `FORECAST_REGIONS=regions_by_model[<model>]`.
5. **per-region downstream** — `for region in keys(regions)`:
   - postprocess gate (always)
   - for `stage in regions[region].stages`: invoke that stage's job (`blend`, `sync`).

Workflow `for` loops are already supported in GCP Workflows YAML. Existing `pipeline_state` and `write_text_object` subroutines kept; only their callers change.

## Critical files to modify

- `terraform/variables.tf`
- `terraform/environments/dev/main.tf`, `terraform/environments/prod/main.tf`
- `terraform/modules/storage/{main,variables,outputs}.tf`
- `terraform/modules/compute/{main,variables,outputs}.tf`
- `terraform/modules/orchestration/{main,variables,outputs}.tf`
- `terraform/modules/orchestration/workflow.yaml.tpl`
- `AIFS/utils/post_process.py` — add `--region` + dispatch; normalize handler signatures.
- `docker/aifs/src/main.py` — loop over `FORECAST_REGIONS`, per-region post-process invocation, per-region marker.
- `docker/neuralgcm/src/main.py` — loop over `FORECAST_REGIONS`, gated full-field upload, per-region marker.
- `docker/downloader/src/main.py` — IC paths under `gs://common/ic/...`.
- `docker/postprocess/src/main.py` — verifier checks markers only.
- `docker/blend/src/main.py` — region-parameterized via `FORECAST_REGION`.
- `docker/sync/src/main.py` — `SYNC_SPEC`-driven, no region names.
- `docker/pipeline-state/src/main.py` — iterates `REGIONS` env var.

Science code: only `AIFS/utils/post_process.py` changes (minor, backward-compatible). `run_model.py`, `run_model_ENS.py`, NeuralGCM science untouched.

## Reused functions / utilities

- `AIFS/utils/post_process.py` — `post_process_india`, `post_process_ethiopia`; reused as-is once signatures normalized.
- `AIFS/utils/run_model.py` and `AIFS/utils/run_model_ENS.py` — both share Docker image; selected by shim.
- `docker/{aifs,neuralgcm}/src/main.py` `_setup_directories`, `_download_inputs`, `_run_science_scripts`, `_upload_outputs` — keep as structural skeleton; rewrite bodies for common + region buckets.
- `download_gcs_file`, `upload_directory`, `read_gcs_text`, `write_gcs_text` helpers — unchanged.
- Workflow `pipeline_state` and `write_text_object` subroutines (`workflow.yaml.tpl:388`, `:415`) — kept; callers change.
- `sync/utils/{drive,sync_config,sync_engine,sync_inventory}.py` and `sync/config/sync.yaml` — unchanged. Cloud sync container plumbs config through; ethiopia rules already exist.

## Implementation order

1. `terraform/variables.tf` + `environments/{dev,prod}/main.tf` — new `regions` map + `upload_neuralgcm_full_field` flag.
2. `terraform/modules/storage/` — common + per-region buckets.
3. `terraform/modules/compute/` — env vars (`GCS_COMMON_BUCKET`, `GCS_REGION_BUCKETS`, `REGIONS`, `REGION_MODELS`, per-job `FORECAST_REGIONS` / `SYNC_SPEC` / `UPLOAD_FULL_FIELD`).
4. `AIFS/utils/post_process.py` — `--region` arg + dispatch table.
5. Docker shims (downloader → aifs → neuralgcm → postprocess → blend → sync → pipeline-state).
6. `terraform/modules/orchestration/workflow.yaml.tpl` — region-agnostic backbone.
7. End-to-end dev verification.

## Verification (end-to-end, dev environment)

1. **Apply terraform** in `terraform/environments/dev/`. Confirm `monsoon-dev-common-{pid}`, `monsoon-dev-region-india-{pid}`, `monsoon-dev-region-ethiopia-{pid}` exist; old `data-` and `weights-` buckets are gone.
2. **Seed weights** by uploading manually to the common bucket:
   - `weights/aifs/aifs-single-mse-1.1.ckpt`
   - `weights/aifs/aifs-ens-crps-1.0.ckpt` (new — required for AIFS-ENS)
   - `weights/aifs/EKR/mir_16_linear/{sparse}.npz`
   - `weights/neuralgcm/...`, `weights/blend/india/...`
3. **Push docker images** (one image for AIFS + AIFS-ENS, plus the others).
4. **Manually trigger workflow** with no region arg.
5. Verify:
   - `gs://monsoon-dev-common-{pid}/ic/ecmwf/{date}/input_state_{date}.pkl` written once (not per region).
   - `gs://monsoon-dev-common-{pid}/ic/ncep/{date}/gdas_{date}.pgrb2` written once.
   - `gs://monsoon-dev-common-{pid}/full_field/aifs/{aifs_date}/init_{aifs_date}.nc` exists.
   - `gs://monsoon-dev-common-{pid}/full_field/aifs_ens/{aifs_date}/init_{aifs_date}.zarr/` exists.
   - `gs://monsoon-dev-common-{pid}/full_field/neuralgcm/{date}/` does NOT exist (flag default false).
   - `gs://monsoon-dev-region-india-{pid}/output/aifs/{aifs_date}/{sji,tcw,tp}/...` exists.
   - `gs://monsoon-dev-region-india-{pid}/output/neuralgcm/{date}/{sji,tcw,tp}/...` exists.
   - `gs://monsoon-dev-region-ethiopia-{pid}/output/aifs/{aifs_date}/AIFS/tp/tp_0p25_{aifs_date}.nc` exists.
   - `gs://monsoon-dev-region-ethiopia-{pid}/output/aifs_ens/{aifs_date}/AIFS_ENS/tp/tp_0p25_{aifs_date}.nc` exists.
   - `gs://monsoon-dev-region-india-{pid}/output/blend/{date}/` populated; `latest.txt` updated.
   - `gs://monsoon-dev-region-ethiopia-{pid}/latest.txt` updated by ethiopia sync.
   - Per-(model, region) markers exist: `intermediate/aifs_india_{aifs_date}_done`, `intermediate/aifs_ethiopia_{aifs_date}_done`, `intermediate/aifs_ens_ethiopia_{aifs_date}_done`, `intermediate/neuralgcm_india_{date}_done`.
6. **Re-run the workflow with the same date**: each model branch should short-circuit on its `{model}_{region}_{date}_done` marker; per-region postprocess gate passes without re-running models. Validates that IC/model state is shared and not re-computed per region.
7. **Curl `pipeline-state`** and confirm the new response shape returns expected presence flags per (model, region).
8. **Toggle `upload_neuralgcm_full_field = true`** and re-apply; rerun NeuralGCM job; confirm `gs://monsoon-dev-common-{pid}/full_field/neuralgcm/{date}/` now exists.
