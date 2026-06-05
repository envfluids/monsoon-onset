# Monsoon Onset Prediction System

This top-level README documents the checked-in HPC operational workflow for this
repository. Cloud documentation is intentionally not duplicated here; see:

- [README_CLOUD.md](README_CLOUD.md)
- [terraform/README.md](terraform/README.md)
- [docker/neuralgcm/README.md](docker/neuralgcm/README.md)

For a hands-on, DSI-specific operations runbook (manual recovery steps, debugging,
example commands for every stage), see:

- [OPERATIONS_MANUAL_DSI.md](OPERATIONS_MANUAL_DSI.md)

## HPC Layout

The HPC workflow is controlled by host-specific shell scripts under `HPC/` and
shared Python orchestration under `HPC/utils/`.

| Path | Purpose |
| --- | --- |
| `.config/config.json` | Selects the active HPC cluster with `cluster`; `cluster_id` is used by sync/live output metadata. |
| `.config/envs.json` | Per-cluster map of conda environment prefixes used by the batch scripts (e.g. `dsi.models.AIFS_single_v2`). |
| `HPC/utils/main.py` | Central orchestration entrypoint. It checks/downloads data and submits the cluster-specific batch script. |
| `HPC/utils/data_listener.py` | Imports downloader functions while preserving each module's local path assumptions. |
| `HPC/utils/job_submitter.py` | Builds and submits batch commands. `dsi` and `midway` use Slurm `sbatch`; `derecho` uses PBS `qsub`. |
| `HPC/dsi/` | DSI Slurm batch and cron scripts. |
| `HPC/midway/` | Midway Slurm batch and cron scripts. |
| `HPC/derecho/` | Derecho PBS batch and cron scripts. |

The active cluster must be one of `dsi`, `midway`, or `derecho`. The central
orchestrator resolves batch scripts from `HPC/{cluster}/`. The repository is
currently configured for `dsi`.

## HPC Orchestrator

Run the central orchestrator from the repository root:

```bash
python ./HPC/utils/main.py --pipelines aifs --dry-run
python ./HPC/utils/main.py --pipelines ngcm --date 20260508T00
python ./HPC/utils/main.py --pipelines imerg
python ./HPC/utils/main.py --pipelines s2s --start-date 20260501 --end-date 20260508
```

Supported `--pipelines` values are:

| Pipeline | Data check/download function | Submitted script(s) | Work directory | Notes |
| --- | --- | --- | --- | --- |
| `aifs` | `IC/utils/download_ecmwf.py:get_data` | `run_AIFS.sh` | `AIFS/utils` | Submits only 00 UTC cycles. One job per model in `AIFS_single_v1p1`, `AIFS_single_v2`. |
| `aifs_ens` | `IC/utils/download_ecmwf.py:get_data` | `run_AIFS_ENS.sh` | `AIFS/utils` | Optional script (one job per model in `AIFS_ENS_v1`, `AIFS_ENS_v2`). Checked in for `dsi`; missing scripts are skipped. |
| `gencast` | `IC/utils/download_ecmwf.py:get_data` | `run_gencast.sh` | `gencast/utils` | Optional. GenCast SST is downloaded by the ECMWF downloader (model config requests an SST `PARAM_MARS`). |
| `ecmwf` | `IC/utils/download_ecmwf.py:get_data` | `run_AIFS.sh`, `run_AIFS_ENS.sh`, `run_gencast.sh` | `AIFS/utils`, `gencast/utils` | Composite pipeline used by the DSI AIFS cron wrapper: it submits the `aifs`, `aifs_ens`, and `gencast` jobs together. |
| `ngcm` | `IC/utils/download_ncep.py:get_data` | `run_NGCM.sh` | `NeuralGCM/utils` | Submits only 00 UTC cycles. |
| `imerg` | `IMERG/utils/download_imerg.py:get_data` | `process_IMERG.sh` | `IMERG/utils` | Also calls `IMERG/utils/download_imd.py:get_imd_data` for the same date before job submission. |
| `s2s` | `S2S/utils/download_forecast.py:get_data` | `process_S2S.sh` | `S2S/utils` | Optional script. Allows 00 and 12 UTC cycles; date ranges are supported only for `s2s`. |

The initial-condition downloaders now live under `IC/utils/`
(`download_ecmwf.py` for AIFS/AIFS_ENS/GenCast, `download_ncep.py` for
NeuralGCM); they expose `get_data` and, for ECMWF, `check_new_data`.

Dates are normalized by `HPC/utils/main.py`:

- Model/S2S dates accept `YYYYMMDD`, `YYYYMMDDHH`, or `YYYYMMDDTHH`; `YYYYMMDD`
  becomes `YYYYMMDDT00`.
- IMERG dates accept `YYYYMMDD`, `YYYYMMDDHH`, or `YYYYMMDDTHH`; the submitted
  date is `YYYYMMDD`.
- `--start-date` and `--end-date` are accepted only with `--pipelines s2s` and
  generate one `YYYYMMDDT00` submission per day.

With `--dry-run`, the orchestrator resolves data and logs the batch command
without creating log directories or submitting to the scheduler.

## Cron Scheduling

Each cluster has a checked-in `cron.txt` containing absolute paths for that
deployment.

| Cluster | Cron file | Current schedule |
| --- | --- | --- |
| `dsi` | `HPC/dsi/cron/cron.txt` | `sync.sh` every 10 minutes, `AIFS.sh` every 15 minutes, `NGCM.sh` every 30 minutes, and `IMERG.sh`, `S2S.sh`, `NCUM.sh` hourly, plus `sync_IITM/utils/cron_job_dsi.sh` every 10 minutes. |
| `midway` | `HPC/midway/cron/cron.txt` | Sync every 10 minutes, AIFS every 15 minutes, NGCM every 30 minutes, IMERG hourly. |
| `derecho` | `HPC/derecho/cron/cron.txt` | Sync every 10 minutes, AIFS every 15 minutes, NGCM every 30 minutes, IMERG hourly. |

Checked-in DSI cron wrappers (each `conda activate`s an env, most hold a
`flock` lock under `/tmp` so ticks can't overlap):

- `HPC/dsi/cron/AIFS.sh` activates
  `/net/scratch2/marchakitus/conda-envs/AIFS_ENS` and runs
  `python ./HPC/utils/main.py --pipelines ecmwf` under a file lock (20-minute
  timeout).
- `HPC/dsi/cron/NGCM.sh` activates
  `/net/scratch2/marchakitus/conda-envs/operational` and runs
  `python ./HPC/utils/main.py --pipelines ngcm` (15-minute timeout).
- `HPC/dsi/cron/IMERG.sh` activates
  `/net/scratch2/marchakitus/conda-envs/operational_pip`, runs `imerg`, then
  `IMD/utils/download_IMD.py`.
- `HPC/dsi/cron/S2S.sh` runs `--pipelines s2s` from `S2S/utils` under a file
  lock (45-minute timeout).
- `HPC/dsi/cron/NCUM.sh` runs `NCUM/utils/main.py` under a file lock (10-minute
  timeout); it downloads the latest NCUM forecast and triggers the India NCUM
  blend when a new one arrives.
- `HPC/dsi/cron/sync.sh` activates
  `/net/scratch2/marchakitus/conda-envs/operational_pip` and runs
  `python ./main.py` from `sync/utils` under a file lock.
- `HPC/midway/cron/cron_job_AIFS.sh`, `cron_job_NGCM.sh`,
  `cron_job_IMERG.sh`, and `cron_job_sync.sh` are checked in.
- `HPC/derecho/cron/cron_job_AIFS.sh`, `cron_job_NGCM.sh`,
  `cron_job_IMERG.sh`, and `cron_job_sync.sh` are checked in and run commands
  through `ssh derecho`.

The checked-in sync entrypoint is `sync/utils/main.py`.

## Batch Scripts

`HPC/utils/job_submitter.py` exports `DATE_F={date}` (and, on Slurm clusters,
`MODEL={label}`) to the submitted batch script. Generated scheduler logs go
under `HPC/{cluster}/logs/{label}/` when submitted through the central
orchestrator.

### DSI

The DSI `run_AIFS.sh` / `run_AIFS_ENS.sh` scripts resolve their conda
environments from `.config/envs.json` (model env for inference, the `default`
env for post-processing and blending).

| Script | Scheduler resources | Main steps |
| --- | --- | --- |
| `HPC/dsi/run_AIFS.sh` | Slurm, `general`, 1 node, 1 task, 8 CPU, 1 A100, 64G, 1 hour | `AIFS/utils/run_model.py`, `post_process.py`, then `blend/utils/main.py`. |
| `HPC/dsi/run_AIFS_ENS.sh` | Slurm, `general`, 1 node, 16 CPU, 4 A100, 200G, 2 hours | `AIFS/utils/run_model_ENS.py`, `post_process.py --region ethiopia`, then `blend/utils/main.py --region ethiopia`. |
| `HPC/dsi/run_gencast.sh` | Slurm, `Monsoon`, 1 node, 32 CPU, 4 H200, 350G, 12 hours | `gencast/utils/run_gencast.py`, then `post_process.py`. Blend is commented out (GenCast blend is diagnostics-only). |
| `HPC/dsi/run_NGCM.sh` | Slurm, `general`, 1 node, 16 CPU, 4 A100, 120G, 1 hour | `NeuralGCM/utils/preprocess_ic.py`, `run_model.py`, `post_process.py`, `post_process_merge.py`, then `blend/utils/main.py`. |
| `HPC/dsi/process_IMERG.sh` | Slurm, `general`, 1 node, 2 CPU, 32G, 30 minutes | `IMERG/utils/plot.py`, then `plot_bias.py`. |
| `HPC/dsi/process_S2S.sh` | Slurm, `general`, 1 node, 2 CPU, 32G, 30 minutes | `S2S/utils/process_forecast.py`. |

### Midway

| Script | Scheduler resources | Main steps |
| --- | --- | --- |
| `HPC/midway/run_AIFS.sh` | Slurm, `pi-pedramh`, `pedramh-gpu`, 1 node, 4 GPUs, 350G, 1 hour | `AIFS/utils/run_model.py`, `post_process.py`, `verify_completion.py`, then `blend/utils/main.py`. |
| `HPC/midway/run_NGCM.sh` | Slurm, `pi-pedramh`, `pedramh-gpu`, 1 node, 32 tasks, 4 GPUs, 350G, 1 hour | `preprocess.py`, `run_model.py`, `post_process.py`, `post_process_merge.py`, `verify_completion.py`, then `blend/utils/main.py`. |
| `HPC/midway/process_IMERG.sh` | Slurm, `pi-pedramh`, `pedramh-gpu`, 1 node, 32 tasks, 350G, 30 minutes | `IMERG/utils/plot.py`. `plot_bias.py` is commented out. |

### Derecho

| Script | Scheduler resources | Main steps |
| --- | --- | --- |
| `HPC/derecho/run_AIFS.sh` | PBS, account `uric0009`, 1 node, 2 CPUs, 1 GPU, 32GB, `develop`, 1 hour | `AIFS/utils/run_model.py`, `post_process.py`, `verify_completion.py`, `blend/utils/main.py`, then `sync/utils/main.py`. |
| `HPC/derecho/run_NGCM.sh` | PBS, account `uric0009`, 1 node, 32 CPUs, 1 GPU, 100GB, `develop`, 1 hour | `preprocess.py`, `run_model.py`, `post_process.py`, `post_process_merge.py`, `verify_completion.py`, `blend/utils/main.py`, then `sync/utils/main.py`. |
| `HPC/derecho/process_IMERG.sh` | PBS, account `uric0009`, 1 node, 2 CPUs, 20GB, `develop`, 30 minutes | `IMERG/utils/plot.py`, then `plot_bias.py`. |
| `HPC/derecho/process_S2S.sh` | PBS, account `uric0009`, 1 node, 1 CPU, 6GB, `develop`, 1 hour | `S2S/utils/process_forecast.py`, then `sync/utils/sync_S2S.py`. |

> **Stale references on `midway`/`derecho`:** their `run_AIFS.sh`/`run_NGCM.sh`
> still call `verify_completion.py` and (for NGCM) `preprocess.py`, but neither
> file exists in the current tree — the actual NeuralGCM preprocessor is
> `preprocess_ic.py` and there is no verification script. The DSI scripts have
> been updated and do not reference either. Treat the midway/derecho scripts as
> needing a refresh before use.

## Model and Product Paths

### AIFS

- Downloader: `IC/utils/download_ecmwf.py`
- Raw initial conditions: `IC/output/ecmwf/{YYYYMMDDHH}0000-0h-{oper,wave}-fc.grib2`
- GenCast SST (when configured): `IC/output/ecmwf/sst_{date}.nc`
- Deterministic raw output: `AIFS/output/raw/{MODEL}/init_{date}.nc`
- Ensemble raw output: `AIFS/output/raw/{MODEL}/init_{date}.zarr`
- Post-processing script: `AIFS/utils/post_process.py`

`AIFS/utils/post_process.py` writes per-model, per-country products:

India (`AIFS/output/india/{MODEL}/`):

- `AIFS/output/india/{MODEL}/sji/sji_{date}.nc`
- `AIFS/output/india/{MODEL}/tcw/tcw_{date}.nc`
- `AIFS/output/india/{MODEL}/tp/tp_2p0_{date}.nc`
- `AIFS/output/india/{MODEL}/tp/tp_0p25_{date}.nc`

Ethiopia (`AIFS/output/ethiopia/{MODEL}/`):

- `AIFS/output/ethiopia/{MODEL}/tp/tp_0p25_{date}.nc`

`{MODEL}` is one of `AIFS_single_v1p1`, `AIFS_single_v2`, `AIFS_ENS_v1`,
`AIFS_ENS_v2`. The regions each model produces are configured in
`config/models.json`; pass `--region india|ethiopia` to `post_process.py` to
limit to one.

### GenCast

- Downloader: `IC/utils/download_ecmwf.py` (SST `PARAM_MARS`)
- Raw output: `gencast/raw/output/init_{date}.zarr`
- Post-processing script: `gencast/utils/post_process.py`
- Product: `gencast/output/ethiopia/tp/tp_0p25_{date}.nc`

### NeuralGCM

- Downloader: `IC/utils/download_ncep.py`
- Downloaded GDAS file: `IC/output/ncep/gdas_{date}.pgrb2`
- Processed initial condition:
  `NeuralGCM/raw/ncep_ic/processed/gdas_{date}.nc`
- Raw ensemble output: `NeuralGCM/output/raw/{date}.zarr`
- Merged India outputs:
  - `NeuralGCM/output/india/sji/sji_{date}.nc`
  - `NeuralGCM/output/india/tcw/tcw_{date}.nc`
  - `NeuralGCM/output/india/tp/tp_2p0_{date}.nc`
- Merged Ethiopia output:
  - `NeuralGCM/output/ethiopia/tp/tp_2p8_{date}.nc`

`NeuralGCM/utils/run_model.py` uses `N_MEMBERS = 30`. A direct invocation
launches one GPU-isolated inference worker per visible GPU. Members are split
into balanced fixed batches, and the writer process buffers results until they
can be appended in ensemble order. `post_process.py` writes per-member
intermediates and `post_process_merge.py` collapses them into the merged
products above (and computes the India SJI); run them in that order.

### IMERG and IMD

- IMERG downloader: `IMERG/utils/download_imerg.py`
- IMD downloader called by the orchestrator: `IMERG/utils/download_imd.py`
- Additional IMD bulletin downloader called by cron:
  `IMD/utils/download_IMD.py`
- Raw IMERG daily files: `IMERG/raw/IMERG_daily/`
- Raw IMD files: `IMERG/raw/IMD/`
- Product script: `IMERG/utils/plot.py`
- Bias plot script: `IMERG/utils/plot_bias.py`
- Output root: `IMERG/output/{YYYYMMDD}/`
- IMD bulletin PDFs: `IMD/output/AIWFB_{YYYYMMDD}.pdf`

`plot_bias.py` compares observed IMERG rainfall against the AIFS and NeuralGCM
forecasts initialized five days earlier, so those model products must exist for
`date − 5` days.

### S2S

- Downloader: `S2S/utils/download_forecast.py` (ECMWF CDS / datastores)
- Raw GRIB files: `S2S/raw/grib/ifs_s2s_init_{date}.grib`
- Processed NetCDF: `S2S/raw/netcdf/ifs_s2s_init_{date}.nc`
- Product script: `S2S/utils/process_forecast.py`
- Output root: `S2S/output/india/{date}/`

### NCUM

- Downloader / orchestrator: `NCUM/utils/main.py` (uses `download_forecast.py`)
- Product: `NCUM/output/precipitation_amount/precipitation_amount_{date}.nc`
- NCUM runs inside the cron process (no Slurm job) and, on a new forecast,
  triggers the India `AIFS_single_v1p1_NCUM` blend.

## Blend

`blend/utils/main.py` is the blend dispatcher. Given a `--date`, it runs every
configured blend whose deterministic and ensemble input files for that date
both exist and whose output directory does not already exist (use `--force` to
rerun). It also drives the matching model diagnostics. Filters include
`--region`, `--model`, `--blend`, `--deterministic_model`, `--ensemble_model`,
and the modes `--blend-only` / `--diagnostics-only`; `--dry-run` prints the
eligible commands without running them.

```bash
python ./blend/utils/main.py --date 20260604T00 --region india --dry-run
python ./blend/utils/main.py --date 20260604T00 --model NeuralGCM
python ./blend/utils/main.py --date 20260604T00 --blend AIFS_single_v1p1_NCUM --force
```

Configured blends:

| Region | Blend name | Deterministic | Ensemble | Implementation | Status |
| --- | --- | --- | --- | --- | --- |
| india | `AIFS_single_v1p1_NCUM` | AIFS_single_v1p1 | NCUM | `blend/utils/india2026/AIFS_NCUM_blend/main.py` | blend |
| india | `AIFS_single_v1p1_NeuralGCM` | AIFS_single_v1p1 | NeuralGCM | `blend/utils/india2026/AIFS_NGCM_blend/main.py` | blend + diagnostics |
| india | `AIFS_single_v2_NeuralGCM` | AIFS_single_v2 | NeuralGCM | `blend/utils/india2026/AIFS_NGCM_blend/main.py` | diagnostics-only |
| ethiopia | `AIFS_single_v1p1_AIFS_ENS_v1` | AIFS_single_v1p1 | AIFS_ENS_v1 | `blend/utils/ethiopia2026/run_pipeline.py` | blend + diagnostics |
| ethiopia | `AIFS_single_v2_AIFS_ENS_v2` | AIFS_single_v2 | AIFS_ENS_v2 | `blend/utils/ethiopia2026/run_pipeline.py` | blend + diagnostics |
| ethiopia | `AIFS_single_v2_NeuralGCM` | AIFS_single_v2 | NeuralGCM | `blend/utils/ethiopia2026/run_pipeline.py` | blend + diagnostics |
| ethiopia | `AIFS_single_v2_gencast` | AIFS_single_v2 | gencast | `blend/utils/ethiopia2026/run_pipeline.py` | diagnostics-only |

Blend outputs are written under `blend/output/{india2026,ethiopia2026}/{date}/{blend_name}/`.
The Ethiopia implementation lives in a git submodule at
`blend/utils/ethiopia2026/operational`; run
`git submodule update --init --recursive` after cloning. The earlier
`blend/utils/india2025/` pipeline is retained as legacy.

## Model Diagnostics

`model_diagnostics/utils/main.py` produces the per-blend diagnostic plots; the
blend dispatcher invokes it with the matching region/models/inputs/output dir.
India helpers (run from `model_diagnostics/`) include
`utils.india.get_subdistrict_rainfall`, `utils.india.onset_subdistrict_criteria`,
and `utils.create_matrix`.

## Sync

Primary sync code is under `sync/`:

- Config: `sync/config/sync.yaml`
- CLI: `sync/utils/main.py`
- Google Drive client: `sync/utils/drive.py`
- Sync engine: `sync/utils/sync_engine.py`
- Inventory database support: `sync/utils/sync_inventory.py`
- State: `sync/state/drive_inventory_{region}_{cluster}.sqlite3`
- Drive auth: `sync/.auth/` (`credentials.json`, auto-refreshed `token.json`)

`sync/utils/main.py` takes an action (`sync`, `reconcile`, `ls-drive`, `live`;
default `sync`) plus filters such as `--region`, `--date`, and `--rule`. The
default `sync` action updates live assets unless `--skip-live` is supplied, then
syncs configured rules to Google Drive.

```bash
python ./sync/utils/main.py sync --region india --date 20260604T00
python ./sync/utils/main.py reconcile --region india --repair-mode upload-missing
python ./sync/utils/main.py ls-drive --region india --date 20260604T00
```

Configured regions/rules in `sync/config/sync.yaml` cover India and Ethiopia
products from AIFS, NeuralGCM, GenCast, the blend outputs, IMERG, S2S, IMD
bulletins, and model diagnostics.

DSI also has a separate IITM sync path that publishes India AIFS `tp` files:

- Cron wrapper: `sync_IITM/utils/cron_job_dsi.sh`
- CLI: `sync_IITM/utils/main.py`
- Drive client: `sync_IITM/utils/drive.py`

## Operational Notes

- Batch scripts assume they are run from the model utility directory specified
  in `HPC/utils/main.py` (the submitter sets the working directory).
- Many model paths are relative to the utility script working directory.
- Conda environment paths are host-specific. On DSI they are resolved from
  `.config/envs.json`; on midway/derecho they are hardcoded in the scripts.
- `s2s` and `gencast` job submission is optional because not every cluster has
  the corresponding checked-in script.
- The current top-level repository contains generated outputs, logs, raw data,
  checkpoints, credentials, and cache files alongside source code; the paths
  above describe the checked-in operational layout as it exists in this tree.
