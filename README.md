# Monsoon Onset Prediction System

This top-level README documents the checked-in HPC operational workflow for this
repository. Cloud documentation is intentionally not duplicated here; see:

- [README_CLOUD.md](README_CLOUD.md)
- [terraform/README.md](terraform/README.md)
- [docker/neuralgcm/README.md](docker/neuralgcm/README.md)

## HPC Layout

The HPC workflow is controlled by host-specific shell scripts under `HPC/` and
shared Python orchestration under `HPC/utils/`.

| Path | Purpose |
| --- | --- |
| `.config/config.json` | Selects the active HPC cluster with `cluster`; `cluster_id` is used by sync/live output metadata. |
| `HPC/utils/main.py` | Central orchestration entrypoint. It checks/downloads data and submits the cluster-specific batch script. |
| `HPC/utils/data_listener.py` | Imports downloader functions while preserving each module's local path assumptions. |
| `HPC/utils/job_submitter.py` | Builds and submits batch commands. `dsi` and `midway` use Slurm `sbatch`; `derecho` uses PBS `qsub`. |
| `HPC/dsi/` | DSI Slurm batch and cron scripts. |
| `HPC/midway/` | Midway Slurm batch and cron scripts. |
| `HPC/derecho/` | Derecho PBS batch and cron scripts. |

The active cluster must be one of `dsi`, `midway`, or `derecho`. The central
orchestrator resolves batch scripts from `HPC/{cluster}/`.

## HPC Orchestrator

Run the central orchestrator from the repository root:

```bash
python ./HPC/utils/main.py --pipelines aifs --dry-run
python ./HPC/utils/main.py --pipelines ngcm --date 20260508T00
python ./HPC/utils/main.py --pipelines imerg
python ./HPC/utils/main.py --pipelines s2s --start-date 20260501 --end-date 20260508
```

Supported `--pipelines` values are:

| Pipeline | Data check/download function | Submitted script | Work directory | Notes |
| --- | --- | --- | --- | --- |
| `aifs` | `AIFS/utils/download_ic.py:get_data` | `run_AIFS.sh` | `AIFS/utils` | Submits only 00 UTC cycles. |
| `aifs_ens` | `AIFS/utils/download_ic.py:get_data` | `run_AIFS_ENS.sh` | `AIFS/utils` | Optional script. It is checked in for `dsi`; missing scripts are skipped. |
| `ecmwf` | `AIFS/utils/download_ic.py:get_data` | `run_AIFS.sh`, `run_AIFS_ENS.sh` | `AIFS/utils` | Composite pipeline used by the DSI AIFS cron wrapper. |
| `ngcm` | `NeuralGCM/utils/download_ncep.py:get_data` | `run_NGCM.sh` | `NeuralGCM/utils` | Submits only 00 UTC cycles. |
| `imerg` | `IMERG/utils/download_imerg.py:get_data` | `process_IMERG.sh` | `IMERG/utils` | Also calls `IMERG/utils/download_imd.py:get_imd_data` for the same date before job submission. |
| `s2s` | `S2S/utils/download_forecast.py:get_data` | `process_S2S.sh` | `S2S/utils` | Optional script. Allows 00 and 12 UTC cycles; date ranges are supported only for `s2s`. |

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
| `dsi` | `HPC/dsi/cron/cron.txt` | `sync.sh` every 10 minutes, `AIFS.sh` every 15 minutes, `NGCM.sh` every 30 minutes, `IMERG.sh` hourly, and `sync_IITM/utils/cron_job_dsi.sh` every 10 minutes. |
| `midway` | `HPC/midway/cron/cron.txt` | Sync every 10 minutes, AIFS every 15 minutes, NGCM every 30 minutes, IMERG hourly. |
| `derecho` | `HPC/derecho/cron/cron.txt` | Sync every 10 minutes, AIFS every 15 minutes, NGCM every 30 minutes, IMERG hourly. |

Checked-in cron wrappers:

- `HPC/dsi/cron/AIFS.sh` activates
  `/net/scratch2/marchakitus/conda-envs/AIFS_ENS` and runs
  `python ./HPC/utils/main.py --pipelines ecmwf` under a file lock.
- `HPC/dsi/cron/NGCM.sh` activates
  `/net/scratch2/marchakitus/conda-envs/operational` and runs
  `python ./HPC/utils/main.py --pipelines ngcm`.
- `HPC/dsi/cron/IMERG.sh` runs `imerg`, then `IMD/utils/download_IMD.py`, then
  `s2s`.
- `HPC/dsi/cron/sync.sh` currently has the `python ./main.py` sync invocation
  commented out.
- `HPC/midway/cron/cron_job_AIFS.sh`, `cron_job_NGCM.sh`,
  `cron_job_IMERG.sh`, and `cron_job_sync.sh` are checked in.
- `HPC/derecho/cron/cron_job_AIFS.sh`, `cron_job_NGCM.sh`,
  `cron_job_IMERG.sh`, and `cron_job_sync.sh` are checked in and run commands
  through `ssh derecho`.

The `midway` and `derecho` `cron.txt` files reference
`sync/utils/cron_job_midway.sh` and `sync/utils/cron_job_derecho.sh`; those
files are not present in the current tree. The checked-in sync entrypoint is
`sync/utils/main.py`.

## Batch Scripts

`HPC/utils/job_submitter.py` exports `DATE_F={date}` to the submitted batch
script. Generated scheduler logs go under `HPC/{cluster}/logs/{label}/` when
submitted through the central orchestrator.

### DSI

| Script | Scheduler resources | Main steps |
| --- | --- | --- |
| `HPC/dsi/run_AIFS.sh` | Slurm, `general`, 1 node, 4 tasks, 1 A100, 64G, 1 hour | `AIFS/utils/run_model.py`, `post_process.py`, `verify_completion.py`, then `blend/utils/main.py`. |
| `HPC/dsi/run_AIFS_ENS.sh` | Slurm, `general`, 1 node, 4 tasks, 1 A100, 64G, 1 hour | `AIFS/utils/run_model_ENS.py`, then `post_process.py --model AIFS_ENS`. Blend and verify are commented out. |
| `HPC/dsi/run_NGCM.sh` | Slurm, `general`, 1 node, 32 tasks, 1 A100, 120G, 1 hour | `NeuralGCM/utils/preprocess.py`, `run_model.py`, `post_process.py`, `post_process_merge.py`, `verify_completion.py`, then `blend/utils/main.py`. |
| `HPC/dsi/process_IMERG.sh` | Slurm, `general`, 1 node, 2 tasks, 32G, 30 minutes | `IMERG/utils/plot.py`, then `plot_bias.py`. |
| `HPC/dsi/process_S2S.sh` | Slurm, `general`, 1 node, 2 tasks, 32G, 30 minutes | `S2S/utils/process_forecast.py`. |

### Midway

| Script | Scheduler resources | Main steps |
| --- | --- | --- |
| `HPC/midway/run_AIFS.sh` | Slurm, `pi-pedramh`, `pedramh-gpu`, 1 node, 4 GPUs, 350G, 1 hour | `AIFS/utils/run_model.py`, `post_process.py`, `verify_completion.py`, then `blend/utils/main.py`. |
| `HPC/midway/run_NGCM.sh` | Slurm, `pi-pedramh`, `pedramh-gpu`, 1 node, 32 tasks, 4 GPUs, 350G, 1 hour | `preprocess.py`, four background `run_model.py --mpi 0..3` processes, `post_process.py`, `post_process_merge.py`, `verify_completion.py`, then `blend/utils/main.py`. |
| `HPC/midway/process_IMERG.sh` | Slurm, `pi-pedramh`, `pedramh-gpu`, 1 node, 32 tasks, 350G, 30 minutes | `IMERG/utils/plot.py`. `plot_bias.py` is commented out. |

### Derecho

| Script | Scheduler resources | Main steps |
| --- | --- | --- |
| `HPC/derecho/run_AIFS.sh` | PBS, account `uric0009`, 1 node, 2 CPUs, 1 GPU, 32GB, `develop`, 1 hour | `AIFS/utils/run_model.py`, `post_process.py`, `verify_completion.py`, `blend/utils/main.py`, then `sync/utils/main.py`. |
| `HPC/derecho/run_NGCM.sh` | PBS, account `uric0009`, 1 node, 32 CPUs, 1 GPU, 100GB, `develop`, 1 hour | `preprocess.py`, `run_model.py`, `post_process.py`, `post_process_merge.py`, `verify_completion.py`, `blend/utils/main.py`, then `sync/utils/main.py`. |
| `HPC/derecho/process_IMERG.sh` | PBS, account `uric0009`, 1 node, 2 CPUs, 20GB, `develop`, 30 minutes | `IMERG/utils/plot.py`, then `plot_bias.py`. |
| `HPC/derecho/process_S2S.sh` | PBS, account `uric0009`, 1 node, 1 CPU, 6GB, `develop`, 1 hour | `S2S/utils/process_forecast.py`, then `sync/utils/sync_S2S.py`. |

## Model and Product Paths

### AIFS

- Downloader: `AIFS/utils/download_ic.py`
- Raw initial conditions: `AIFS/raw/ifs_ic/input_state_{date}.pkl`
- Deterministic raw output: `AIFS/raw/output/AIFS/init_{date}.nc`
- Ensemble raw output: `AIFS/raw/output/AIFS_ENS/init_{date}.zarr`
- Post-processing script: `AIFS/utils/post_process.py`
- Verification script: `AIFS/utils/verify_completion.py`

Current `AIFS/utils/post_process.py` writes India products under
`AIFS/output/india/`:

- `AIFS/output/india/sji/sji_{date}.nc`
- `AIFS/output/india/tcw/tcw_{date}.nc`
- `AIFS/output/india/tp/tp_2p0_{date}.nc`
- `AIFS/output/india/tp/tp_0p25_{date}.nc`
- `AIFS/output/india/AIFS/tp/tp_0p25_{date}.nc`
- `AIFS/output/india/AIFS_ENS/tp/tp_0p25_{date}.nc`

The AIFS and NeuralGCM verification scripts currently check for AIFS outputs
under `AIFS/output/{tp,sji,tcw}/`, not `AIFS/output/india/{tp,sji,tcw}/`.

### NeuralGCM

- Downloader: `NeuralGCM/utils/download_ncep.py`
- Downloaded GDAS file: `NeuralGCM/raw/ncep_ic/download/gdas_{date}.pgrb2`
- Processed initial condition:
  `NeuralGCM/raw/ncep_ic/processed/gdas_{date}.nc`
- Raw member output: `NeuralGCM/raw/output/{date}/member_{member}.zarr`
- Merged outputs:
  - `NeuralGCM/output/sji/sji_{date}.nc`
  - `NeuralGCM/output/tcw/tcw_{date}.nc`
  - `NeuralGCM/output/tp/tp_{date}.nc`

`NeuralGCM/utils/run_model.py` uses `N_MEMBERS = 30`. On Midway the batch
script launches four `--mpi` ranks; on DSI and Derecho the checked-in scripts
run one process without `--mpi`.

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

### S2S

- Downloader: `S2S/utils/download_forecast.py`
- Raw GRIB files: `S2S/raw/grib/ifs_s2s_{cf|pf}_init_{date}.grib`
- Processed NetCDF: `S2S/raw/netcdf/ifs_s2s_init_{date}.nc`
- Product script: `S2S/utils/process_forecast.py`
- Output root: `S2S/output/{date}/`

## Blend

The checked-in blend implementation lives under `blend/utils/india2025/`:

- `blend/utils/india2025/main.py`
- `blend/utils/india2025/aifs.py`
- `blend/utils/india2025/ngcm.py`
- `blend/utils/india2025/blend.py`
- `blend/utils/india2025/maps.py`
- `blend/utils/india2025/messages.py`

The HPC AIFS and NeuralGCM batch scripts currently run:

```bash
cd ../../blend/utils
python ./main.py --date $DATE_F
```

There is no `blend/utils/main.py` checked into the current tree, so the README
does not describe that invocation as a valid checked-in entrypoint.

## Sync

Primary sync code is under `sync/`:

- Config: `sync/config/sync.yaml`
- CLI: `sync/utils/main.py`
- Google Drive client: `sync/utils/drive.py`
- Sync engine: `sync/utils/sync_engine.py`
- Inventory database support: `sync/utils/sync_inventory.py`

Default `python sync/utils/main.py` action is `sync`. It updates live assets
unless `--skip-live` is supplied, then syncs configured rules to Google Drive.

Configured rules in `sync/config/sync.yaml` cover:

- AIFS files from `AIFS/output`
- NeuralGCM files from `NeuralGCM/output`
- Blend dated directories from `blend/output`
- Google blend dated directories from `blend/output_google`
- IMERG dated directories from `IMERG/output`
- S2S dated directories from `S2S/output`
- IMD bulletin PDFs from `IMD/output`

DSI also has a separate IITM sync path:

- Cron wrapper: `sync_IITM/utils/cron_job_dsi.sh`
- CLI: `sync_IITM/utils/main.py`
- Drive client: `sync_IITM/utils/drive.py`

## Operational Notes

- Batch scripts assume they are run from the model utility directory specified
  in `HPC/utils/main.py`.
- Many model paths are relative to the utility script working directory.
- Conda environment paths are host-specific and hardcoded in the cron and batch
  scripts.
- `AIFS` and `NGCM` verification requires both model families' expected output
  files for the same `DATE_F`.
- `s2s` job submission is optional because not every cluster has a checked-in
  `process_S2S.sh`.
- The current top-level repository contains generated outputs, logs, raw data,
  checkpoints, credentials, and cache files alongside source code; the paths
  above describe the checked-in operational layout as it exists in this tree.
