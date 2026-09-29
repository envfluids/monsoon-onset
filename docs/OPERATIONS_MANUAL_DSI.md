# Monsoon-Onset Operations Manual — DSI Cluster

**Audience:** whoever is on call for the operational monsoon-onset forecast pipeline.
**Scope:** the **DSI** cluster only (`cluster = "dsi"` in `.config/config.json`). Midway and
Derecho have their own scripts under `HPC/midway/` and `HPC/derecho/`; this manual does **not**
cover them.

This document tells you (1) what runs automatically and when, (2) how every step works end to
end, (3) how to run **any single step by hand** when the automation fails, and (4) how to debug
the most common failures. Read sections 1–3 once; keep sections 8–10 open when something breaks.

> **Repo root on DSI:** `/net/monsoon/operational/monsoon-onset`
> Every relative path below is relative to that root. All example commands assume you have
> `cd`-ed there first unless stated otherwise.

---

## 0. The 60-second mental model

```
                         ┌──────────────── cron (every 10–60 min) ────────────────┐
                         │                                                        │
  ECMWF / NCEP / IMERG   │   HPC/utils/main.py  ──checks for new data──►  sbatch  │
  / IMD / NCMRWF  ──────►│   (the "orchestrator")        job on a GPU/CPU node    │
                         │                                                        │
                         └────────────────────────────────────────────────────────┘
                                               │
        ┌──────────────────────────────────────┼───────────────────────────────────────┐
        ▼                  ▼                   ▼                ▼                      ▼
   download IC        run model         post-process       BLEND          model_diagnostics
 (IC/utils/*.py)   (run_model.py)    (post_process.py)  (blend/utils/    (model_diagnostics/
                                                          main.py)         utils/main.py)
                                               │
                                               ▼
                                      sync/utils/main.py  ──►  Google Drive
```

Five data streams flow through this every day:

| Stream | Model(s) | Countries | Cadence (cron) |
|---|---|---|---|
| **AIFS** (+ AIFS_ENS, GenCast) | ECMWF AIFS | India, Ethiopia | every 15 min |
| **NeuralGCM** | NeuralGCM | India, Ethiopia | every 30 min |
| **IMERG / IMD** | observations | India | hourly |
| **S2S** | ECMWF S2S | (global → India) | hourly |
| **NCUM** | NCMRWF NCUM | India | hourly |

The cron jobs are **idempotent and self-checking**: each one asks "is there new data I haven't
processed?" and exits quietly if not. That is why most of operations is just *letting it run* and
only intervening when a date is missing or a job errored.

---

## 1. Orientation: config, environments, key paths

### 1.1 Cluster selector

`.config/config.json`:

```json
{"cluster": "dsi", "cluster_id": "C"}
```

`cluster` tells `HPC/utils/main.py` and `job_submitter.py` to use `sbatch` (Slurm) and to read
scripts from `HPC/dsi/`. `cluster_id` ("C") is stamped into sync/live metadata. **Do not change
these on the DSI box.**

### 1.2 Conda environments (DSI)

Environment paths are **not** on your `$PATH` by name — they are absolute prefixes. The batch
scripts look them up from `.config/envs.json`:

```json
{
  "dsi": {
    "models": {
      "AIFS_single_v1p1": "/net/scratch2/marchakitus/conda-envs/AIFS_ENS",
      "AIFS_ENS_v1":      "/net/scratch2/marchakitus/conda-envs/AIFS_ENS",
      "AIFS_single_v2":   "/net/scratch2/marchakitus/conda-envs/AIFSv2",
      "AIFS_ENS_v2":      "/net/scratch2/marchakitus/conda-envs/AIFS_ENSv2",
      "NeuralGCM":        "/net/scratch2/marchakitus/conda-envs/neuralgcm",
      "gencast":          "/net/scratch/marchakitus/conda-envs/gencast"
    },
    "IC": {
      "ecmwf": "/net/scratch2/marchakitus/conda-envs/AIFS_ENS",
      "ncep":  "/net/scratch2/marchakitus/conda-envs/operational"
    },
    "misc": {
      "sync":      "/net/scratch2/marchakitus/conda-envs/operational_pip",
      "sync_IITM": "/net/scratch2/marchakitus/conda-envs/operational_pip",
      "default":   "/net/scratch2/marchakitus/conda-envs/operational"
    }
  }
}
```

**Cheat sheet — which env for which task:**

| Task | Activate |
|---|---|
| Download ECMWF IC, run AIFS v1 (det/ens) | `/net/scratch2/marchakitus/conda-envs/AIFS_ENS` |
| Run AIFS v2 deterministic | `/net/scratch2/marchakitus/conda-envs/AIFSv2` |
| Run AIFS v2 ensemble | `/net/scratch2/marchakitus/conda-envs/AIFS_ENSv2` |
| Run NeuralGCM inference | `/net/scratch2/marchakitus/conda-envs/neuralgcm` |
| Run GenCast inference | `/net/scratch/marchakitus/conda-envs/gencast` |
| **Post-process, blend, diagnostics, NGCM IC, S2S, NCUM, default** | `/net/scratch2/marchakitus/conda-envs/operational` |
| Sync to Google Drive (main + IITM), IMERG cron | `/net/scratch2/marchakitus/conda-envs/operational_pip` |

To activate one in an interactive shell:

```bash
eval "$(conda shell.bash hook)"
conda activate /net/scratch2/marchakitus/conda-envs/operational
```

### 1.3 Repository layout

| Path | What lives there |
|---|---|
| `HPC/utils/main.py` | The orchestrator (data check + job submission) |
| `HPC/utils/job_submitter.py` | Builds the `sbatch` command, retries 5× |
| `HPC/dsi/run_*.sh`, `process_*.sh` | Slurm batch scripts (the actual work) |
| `HPC/dsi/cron/*.sh`, `cron.txt` | Cron wrappers + the crontab |
| `HPC/dsi/logs/{LABEL}/` | Slurm stdout/stderr per job (`.o<jobid>`, `.e<jobid>`) |
| `HPC/dsi/cron/logs/*.log` | Cron wrapper output (one file per stream) |
| `IC/utils/` | `download_ecmwf.py`, `download_ncep.py` (initial conditions) |
| `AIFS/`, `NeuralGCM/`, `gencast/` | Model code, weights, `raw/` inputs, `output/` products |
| `IMERG/`, `IMD/`, `S2S/`, `NCUM/` | Observation + secondary-forecast streams |
| `blend/utils/main.py` | Blend dispatcher (decides which country/model blends to run) |
| `blend/utils/{india2026,ethiopia2026}/` | Per-country blend implementations |
| `model_diagnostics/utils/main.py` | Diagnostics dispatcher (India/Ethiopia plots) |
| `sync/`, `sync_IITM/` | Google Drive sync |
| `config/models.json` | Model definitions (weights, params, regions) |
| `.config/envs.json` | Conda env map (above) |

### 1.4 Date formats — read this once

The orchestrator normalizes whatever you pass:

- **Model / S2S dates:** `YYYYMMDD`, `YYYYMMDDHH`, or `YYYYMMDDTHH`. A bare `YYYYMMDD` becomes
  `YYYYMMDDT00`. The canonical form used everywhere downstream is **`YYYYMMDDT00`** (e.g.
  `20260604T00`).
- **IMERG dates:** reduced to `YYYYMMDD` (e.g. `20260604`).
- Only **00 UTC** cycles are submitted for AIFS / NGCM / GenCast. S2S allows **00 and 12**.

Throughout this manual, `$DATE_F` means a forecast date like `20260604T00`.

---

## 2. What runs automatically (the DSI crontab)

`HPC/dsi/cron/cron.txt` — install with `crontab HPC/dsi/cron/cron.txt` (verify with `crontab -l`):

```
*/10 * * * *  …/HPC/dsi/cron/sync.sh   >> …/cron/logs/sync.log  2>&1
*/15 * * * *  …/HPC/dsi/cron/AIFS.sh   >> …/cron/logs/AIFS.log  2>&1
*/30 * * * *  …/HPC/dsi/cron/NGCM.sh   >> …/cron/logs/NGCM.log  2>&1
0   * * * *  …/HPC/dsi/cron/IMERG.sh  >> …/cron/logs/IMERG.log 2>&1
0   * * * *  …/HPC/dsi/cron/S2S.sh    >> …/cron/logs/S2S.log   2>&1
0   * * * *  …/HPC/dsi/cron/NCUM.sh   >> …/cron/logs/NCUM.log  2>&1
*/10 * * * *  …/sync_IITM/utils/cron_job_dsi.sh >> …/sync_IITM/logs/cron.log 2>&1
```

**Note that you do not need to install this yourself — the DSI box should already have it set up.** The cron daemon will run these scripts at the specified cadence (e.g. every 15 min for AIFS, every hour on the hour for IMERG). Each script is a wrapper that (1) checks for new data, (2) if found, calls `HPC/utils/main.py` with the appropriate pipeline and date, and (3) logs the output to `HPC/dsi/cron/logs/<STREAM>.log`.

What each wrapper does (all of them `conda activate` first, most hold a `flock` lock in `/tmp` so
two ticks can't overlap):

| Wrapper | Env | Command | Timeout / lock |
|---|---|---|---|
| `AIFS.sh` | `AIFS_ENS` | `python ./HPC/utils/main.py --pipelines ecmwf` | 1200 s, lock `monsoon-aifs-dsi.lock` |
| `NGCM.sh` | `operational` | `python ./HPC/utils/main.py --pipelines ngcm` | 15 min, no lock |
| `IMERG.sh` | `operational_pip` | `--pipelines imerg`, then `IMD/utils/download_IMD.py` | 10 min each |
| `S2S.sh` | `operational` | `--pipelines s2s` (run from `S2S/utils`) | 45 min, lock `monsoon-s2s-dsi.lock` |
| `NCUM.sh` | `operational` | `NCUM/utils/main.py` | 600 s, lock `monsoon-ncum-dsi.lock` |
| `sync.sh` | `operational_pip` | `sync/utils/main.py` (full Drive sync) | lock `monsoon-sync-dsi.lock` |
| `sync_IITM/utils/cron_job_dsi.sh` | `operational_pip` | `sync_IITM/utils/main.py` | 590 s |

> The `ecmwf` pipeline is a **composite**: one cron tick of `AIFS.sh` checks ECMWF once and, if a
> new 00Z cycle exists, submits AIFS deterministic **and** AIFS ensemble **and** GenCast jobs.

**First thing to check when "nothing is updating":** is cron even installed and is the box up?

```bash
crontab -l | grep monsoon            # are the entries present?
tail -n 40 HPC/dsi/cron/logs/AIFS.log   # what did the last ticks say?
```

A healthy "no new data" tick logs something like
`Will not submit aifs; no new data was found.` — that is **not** an error.

---

## 3. The orchestrator — `HPC/utils/main.py`

This is the single entrypoint the cron wrappers call. You can call it by hand exactly the same
way. It (1) resolves a date (either `--date` you give it, or the latest available upstream), (2)
checks whether that date should be submitted, and (3) submits the Slurm job(s) via `sbatch`.

```bash
# Always run from the repo root:
cd /net/monsoon/operational/monsoon-onset
eval "$(conda shell.bash hook)"
conda activate /net/scratch2/marchakitus/conda-envs/operational
```

### 3.1 Usage

```
python ./HPC/utils/main.py --pipelines <name> [<name> ...]
                           [--date YYYYMMDD[HH|THH]]
                           [--start-date YYYYMMDD --end-date YYYYMMDD]   # s2s only
                           [--dry-run]
```

`--pipelines` accepts: `aifs`, `aifs_ens`, `ecmwf` (= aifs + aifs_ens + gencast), `gencast`,
`ngcm`, `imerg`, `s2s`.

### 3.2 The commands you will actually type

```bash
# See what WOULD be submitted for the latest cycle, without submitting (safe to run anytime):
python ./HPC/utils/main.py --pipelines aifs --dry-run

# Force a specific AIFS+ENS+GenCast date (most common manual recovery):
python ./HPC/utils/main.py --pipelines ecmwf --date 20260604T00

# Re-run NeuralGCM for a specific date:
python ./HPC/utils/main.py --pipelines ngcm --date 20260604T00

# IMERG for a day (also fires the IMD companion download automatically):
python ./HPC/utils/main.py --pipelines imerg --date 20260604

# S2S single date, or a back-fill range (range is S2S-only):
python ./HPC/utils/main.py --pipelines s2s --date 20260604T00
python ./HPC/utils/main.py --pipelines s2s --start-date 20260601 --end-date 20260604
```

**Expected output:** lines like `Cluster: dsi`, `Using explicit date for ecmwf: 20260604T00`,
then for each model a `Successfully submitted job <jobid> for AIFS_single_v2 …`. With `--dry-run`
you instead see `Dry run: would submit sbatch …` and **no** job is queued.

### 3.3 How it decides not to submit (so you know why a date was skipped)

`should_submit()` in `HPC/utils/main.py` will quietly decline when:

- **No new data** was found upstream (and you didn't pass `--date`).
- The cycle **hour isn't allowed** (e.g. an 06Z or 18Z AIFS cycle — only 00 is allowed; S2S also
  allows 12).
- For **S2S without an explicit date**, the output dir `S2S/output/india/<date>` already exists.

If you need to force past a skip, pass `--date` explicitly. For blends/diagnostics there is a
separate `--force` (section 6).

### 3.4 What "submit" actually builds

`job_submitter.py` builds, for each model label:

```
sbatch --job-name=<LABEL>_<DATE_F> \
       --output=HPC/dsi/logs/<LABEL>/<LABEL>_<DATE_F>.o%j \
       --error=HPC/dsi/logs/<LABEL>/<LABEL>_<DATE_F>.e%j \
       --export=DATE_F=<DATE_F>,MODEL=<LABEL> \
       HPC/dsi/run_<...>.sh
```

So the batch script receives **`$DATE_F`** and **`$MODEL`** as environment variables. Submission
is retried up to 5× with exponential backoff; a failure to queue is logged loudly.

---

## 4. End-to-end pipelines & how to run each step by hand

Each subsection lists the automatic path, then the **manual** equivalent for every step so you can
restart from wherever it broke. The golden rule for manual runs:

> Activate the env from the table in §1.2, `cd` into the model's `utils/` directory, then run the
> Python script with `--date $DATE_F` (and `--model` where required).

### 4.1 AIFS (deterministic) — `HPC/dsi/run_AIFS.sh`

**Resources:** Slurm `general`, 1 node, 1× A100, 8 CPU, 64 GB, 1 h.
**Models:** `AIFS_single_v1p1`, `AIFS_single_v2` (the `aifs` pipeline submits one job per model).

Batch sequence (env switches handled inside the script via `envs.json`):

```bash
# 1. model env → inference
python ./run_model.py     --date $DATE_F --model $MODEL
# 2. default env → post-process to products
python ./post_process.py  --date $DATE_F --model $MODEL
# 3. blend dispatcher
cd ../../blend/utils
python ./main.py          --date $DATE_F --model $MODEL
```

**Manual, step by step** (example `AIFS_single_v2`, `20260604T00`):

```bash
cd /net/monsoon/operational/monsoon-onset
eval "$(conda shell.bash hook)"

# Step 0 — initial conditions (shared by all AIFS/GenCast models; see §4.6)
conda activate /net/scratch2/marchakitus/conda-envs/AIFS_ENS
python ./IC/utils/download_ecmwf.py --date 20260604T00
#   → IC/output/ecmwf/202606040000-0h-oper-fc.grib2  (+ wave / sst as configured)

# Step 1 — inference (env depends on model: AIFSv2 here)
cd AIFS/utils
conda activate /net/scratch2/marchakitus/conda-envs/AIFSv2
python ./run_model.py --date 20260604T00 --model AIFS_single_v2
#   → AIFS/output/raw/AIFS_single_v2/init_20260604T00.nc

# Step 2 — post-process (default/operational env)
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date 20260604T00 --model AIFS_single_v2
#   → AIFS/output/india/AIFS_single_v2/{sji,tcw,tp}/...   (tp_2p0, tp_0p25)
#   → AIFS/output/ethiopia/AIFS_single_v2/tp/tp_0p25_20260604T00.nc
#   (use --region india|ethiopia to limit to one country)

# Step 3 — blend (see §6)
cd ../../blend/utils
python ./main.py --date 20260604T00 --model AIFS_single_v2
```

To re-submit the whole thing through Slurm instead of by hand:

```bash
cd HPC/dsi
sbatch --export=DATE_F=20260604T00,MODEL=AIFS_single_v2 \
       --job-name=AIFS_single_v2_20260604T00 \
       --output=logs/AIFS_single_v2/AIFS_single_v2_20260604T00.o%j \
       --error=logs/AIFS_single_v2/AIFS_single_v2_20260604T00.e%j \
       run_AIFS.sh
# …or simply: python ../utils/main.py --pipelines aifs --date 20260604T00
```

### 4.2 AIFS ensemble — `HPC/dsi/run_AIFS_ENS.sh`

**Resources:** Slurm `general`, 1 node, **4× A100**, 16 CPU, 200 GB, 2 h.
**Models:** `AIFS_ENS_v1`, `AIFS_ENS_v2`. **Ethiopia only** for post-process/blend.

Batch sequence:

```bash
python ./run_model_ENS.py --date $DATE_F --model $MODEL
python ./post_process.py  --date $DATE_F --model $MODEL --region ethiopia
cd ../../blend/utils
python ./main.py          --date $DATE_F --region ethiopia --model $MODEL
```

Manual (example `AIFS_ENS_v2`):

```bash
cd AIFS/utils
conda activate /net/scratch2/marchakitus/conda-envs/AIFS_ENSv2
python ./run_model_ENS.py --date 20260604T00 --model AIFS_ENS_v2
#   → AIFS/output/raw/AIFS_ENS_v2/init_20260604T00.zarr   (large, ensemble Zarr)

conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date 20260604T00 --model AIFS_ENS_v2 --region ethiopia
#   → AIFS/output/ethiopia/AIFS_ENS_v2/tp/tp_0p25_20260604T00.nc
```

The ensemble job is the heaviest GPU job (4 GPUs, big Zarr output). If it times out at 2 h, it is
usually GPU contention — check `squeue` and resubmit when GPUs free up.

### 4.3 GenCast — `HPC/dsi/run_gencast.sh`

**Resources:** Slurm partition **`Monsoon`**, 1 node, **4× H200**, 32 CPU, 350 GB, **12 h**.
Submitted as part of the `ecmwf` composite (optional — skipped if SST/data not ready).

Batch sequence:

```bash
unset LD_LIBRARY_PATH                      # important: avoids CUDA/JAX lib clashes
conda activate /net/scratch/marchakitus/conda-envs/gencast
python ./run_gencast.py  --date $DATE_F
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date $DATE_F
# (blend step is commented out in the script — GenCast blend is diagnostics-only)
```

Manual:

```bash
cd gencast/utils
eval "$(conda shell.bash hook)"
unset LD_LIBRARY_PATH
conda activate /net/scratch/marchakitus/conda-envs/gencast
python ./run_gencast.py --date 20260604T00
#   → gencast/raw/output/init_20260604T00.zarr

conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date 20260604T00
#   → gencast/output/ethiopia/tp/tp_0p25_20260604T00.nc
```

> GenCast SST input is downloaded by `IC/utils/download_ecmwf.py` (the model config requests an
> SST `PARAM_MARS`). If GenCast fails immediately on missing SST, re-run the IC download first.

### 4.4 NeuralGCM — `HPC/dsi/run_NGCM.sh`

**Resources:** Slurm `general`, 1 node, **4× A100**, 16 CPU, 120 GB, 1 h.
**Countries:** India + Ethiopia. **Ensemble of 30 members.**

Batch sequence (note the env hops: operational → neuralgcm → operational):

```bash
conda activate …/operational
python ./preprocess_ic.py      --date $DATE_F
conda activate …/neuralgcm
python ./run_model.py          --date $DATE_F
conda activate …/operational
python ./post_process.py       --date $DATE_F
python ./post_process_merge.py --date $DATE_F
cd ../../blend/utils
python ./main.py               --date $DATE_F --model NeuralGCM
```

Manual, step by step:

```bash
cd /net/monsoon/operational/monsoon-onset
eval "$(conda shell.bash hook)"

# Step 0 — GDAS/NCEP initial conditions (00Z only); see §4.6
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./IC/utils/download_ncep.py --date 20260604T00
#   → IC/output/ncep/gdas_20260604T00.pgrb2

cd NeuralGCM/utils
# Step 1 — preprocess IC (interpolation; needs the operational env)
python ./preprocess_ic.py --date 20260604T00
#   → NeuralGCM/raw/ncep_ic/processed/gdas_20260604T00.nc

# Step 2 — ensemble inference (needs 4 GPUs)
conda activate /net/scratch2/marchakitus/conda-envs/neuralgcm
python ./run_model.py --date 20260604T00
#   → NeuralGCM/output/raw/20260604T00.zarr

# Step 3 — per-member post-process, then merge across members
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py       --date 20260604T00
python ./post_process_merge.py --date 20260604T00
#   → NeuralGCM/output/india/{sji/sji,tcw/tcw,tp/tp_2p0}_20260604T00.nc
#   → NeuralGCM/output/ethiopia/tp/tp_2p8_20260604T00.nc

# Step 4 — blend (see §6)
cd ../../blend/utils
python ./main.py --date 20260604T00 --model NeuralGCM
```

> **Order matters:** `post_process.py` writes per-member intermediates; `post_process_merge.py`
> collapses them into the final `tp_2p0` / `tcw` / `sji` files and (for India) computes the SJI.
> If you only re-run merge without the per-member step, it will fail on missing intermediates.

### 4.5 IMERG + IMD (observations) — `HPC/dsi/process_IMERG.sh`

**Resources:** Slurm `general`, 1 node, 2 CPU, 32 GB, 30 min.
The `imerg` orchestrator pipeline also calls `IMERG/utils/download_imd.py` for the same date
before submitting, and the IMERG **cron** additionally runs `IMD/utils/download_IMD.py` (the PDF
bulletin).

Batch sequence:

```bash
python ./plot.py      --date $DATE_F      # IMERG + IMD onset maps, timeseries, CSVs
python ./plot_bias.py --date $DATE_F      # IMERG vs NGCM/AIFS 5-day bias maps
```

Manual:

```bash
cd /net/monsoon/operational/monsoon-onset
eval "$(conda shell.bash hook)"
conda activate /net/scratch2/marchakitus/conda-envs/operational_pip

# Downloads (the orchestrator normally does these for you):
python ./IC/utils/../IMERG/utils/download_imerg.py    # or use the orchestrator (below)
python ./HPC/utils/main.py --pipelines imerg --date 20260604   # download + submit in one shot

# Bulletin PDF (separate; run from IMD/utils):
cd IMD/utils && python ./download_IMD.py            # → IMD/output/AIWFB_<YYYYMMDD>.pdf

# Plots by hand (operational env):
cd ../../IMERG/utils
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./plot.py      --date 20260604
python ./plot_bias.py --date 20260604
#   → IMERG/output/20260604/*.png + onset CSVs
```

> `plot_bias.py` reads the AIFS and NeuralGCM forecasts initialized **5 days earlier**. If those
> model products are missing for `date − 5`, the bias panel will be empty or error — that's a
> missing-upstream problem, not an IMERG bug.

### 4.6 Initial conditions in detail (`IC/utils/`)

Both downloaders are also importable functions (`get_data`, `check_new_data`) — that's how the
orchestrator calls them — but you can run them as scripts:

**ECMWF (for AIFS / AIFS_ENS / GenCast):** `IC/utils/download_ecmwf.py`
- Source: ECMWF **open-data**, `source="aws"` (`DATE_SOURCE = "aws"`), falls back if AWS is down.
- `check_new_data()` returns the latest published cycle; `get_data()` downloads the GRIB streams
  (`oper`, plus `wave` for v2) and SST (for GenCast) defined per-model in `config/models.json`.
- Output: `IC/output/ecmwf/<YYYYMMDDHH>0000-0h-<stream>-fc.grib2` (+ SST `.nc`).
- Retries each URL up to 25× with backoff.

```bash
conda activate /net/scratch2/marchakitus/conda-envs/AIFS_ENS
python ./IC/utils/download_ecmwf.py --date 20260604T00     # omit --date for "latest"
```

**NCEP/GDAS (for NeuralGCM):** `IC/utils/download_ncep.py`
- Source: NOMADS `gdas.t00z.pgrb2.0p25.f000`. **00Z only** (`CYCLE_HOURS = [0]`); checks back up
  to 6 cycles when no date is given.
- Output: `IC/output/ncep/gdas_<YYYYMMDDTHH>.pgrb2`.

```bash
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./IC/utils/download_ncep.py --date 20260604T00
```

### 4.7 S2S — `HPC/dsi/process_S2S.sh`

**Resources:** Slurm `general`, 1 node, 2 CPU, 32 GB, 30 min. Allows **00 and 12** UTC.

```bash
# orchestrator path (download via ecmwf datastores + submit):
python ./HPC/utils/main.py --pipelines s2s --date 20260604T00

# manual processing once the GRIB is downloaded:
cd S2S/utils
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./process_forecast.py --date 20260604T00
#   → S2S/raw/netcdf/ifs_s2s_init_20260604T00.nc, products under S2S/output/india/<date>/
```

The S2S downloader uses the **ECMWF CDS / datastores** client and your `~/.cdsapirc` credentials.
If downloads 401/403, the credentials are the first thing to check.

### 4.8 NCUM — `NCUM/utils/main.py` (no Slurm job; runs in the cron process)

NCUM is lightweight and runs entirely inside the cron wrapper (`NCUM.sh`, env `operational`,
10-min timeout). `NCUM/utils/main.py` downloads the latest NCMRWF precip forecast and, when a new
one arrives, triggers the India `AIFS_single_v1p1_NCUM` blend.

```bash
cd /net/monsoon/operational/monsoon-onset/NCUM/utils
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./main.py                       # auto-detect latest + blend if new
python ./main.py --date 20260604T00    # force a specific date
#   → NCUM/output/precipitation_amount/precipitation_amount_20260604T00.nc
```

The NCMRWF API key lives in `NCUM/.auth/keys.json`. A 0-byte or HTML download usually means an
expired/blocked key or the date not being published yet.

---

## 5. Products & paths (where to look to confirm a date succeeded)

Quick existence check for a date — this is the fastest "did it work?" probe:

```bash
DATE=20260604T00
ls -la AIFS/output/india/AIFS_single_v2/tp/tp_0p25_$DATE.nc \
       AIFS/output/india/AIFS_single_v1p1/tp/tp_0p25_$DATE.nc \
       NeuralGCM/output/india/tp/tp_2p0_$DATE.nc \
       NeuralGCM/output/ethiopia/tp/tp_2p8_$DATE.nc \
       AIFS/output/ethiopia/AIFS_ENS_v2/tp/tp_0p25_$DATE.nc 2>&1
```

| Stream | Key product paths (per `$DATE_F` = `YYYYMMDDT00`) |
|---|---|
| AIFS det (India) | `AIFS/output/india/<MODEL>/{sji/sji,tcw/tcw,tp/tp_2p0,tp/tp_0p25}_<date>.nc` |
| AIFS det (Ethiopia) | `AIFS/output/ethiopia/<MODEL>/tp/tp_0p25_<date>.nc` |
| AIFS ENS (Ethiopia) | `AIFS/output/ethiopia/<MODEL>/tp/tp_0p25_<date>.nc` |
| NeuralGCM (India) | `NeuralGCM/output/india/{sji,tcw,tp}/...tp_2p0_<date>.nc` |
| NeuralGCM (Ethiopia) | `NeuralGCM/output/ethiopia/tp/tp_2p8_<date>.nc` |
| GenCast (Ethiopia) | `gencast/output/ethiopia/tp/tp_0p25_<date>.nc` |
| NCUM (India) | `NCUM/output/precipitation_amount/precipitation_amount_<date>.nc` |
| IMERG/IMD | `IMERG/output/<YYYYMMDD>/*.png`, `*.csv`; bulletin `IMD/output/AIWFB_<YYYYMMDD>.pdf` |
| S2S | `S2S/output/india/<date>/...` |
| Blend | `blend/output/{india2026,ethiopia2026}/<date>/<blend_name>/...` |

`<MODEL>` ∈ {`AIFS_single_v1p1`, `AIFS_single_v2`, `AIFS_ENS_v1`, `AIFS_ENS_v2`}.

---

## 6. Blending — per country (`blend/utils/main.py`)

This is the dispatcher every model script calls after post-processing. It owns the full table of
**which deterministic+ensemble pairs blend, for which country, from which input files**. It is
**self-checking**: it only runs a blend whose two input files both exist and whose output dir
doesn't already exist (unless `--force`).

### 6.1 Usage

```
python ./blend/utils/main.py --date YYYYMMDDTHH
        [--region india|ethiopia] [--model <MODEL>] [--blend <NAME>]
        [--deterministic_model M] [--ensemble_model M]
        [--blend-only | --diagnostics-only]
        [--dry-run] [--force] [--debug] [--skip_to N]
```

- `--model` is how the model scripts call it ("model `X` just finished — run any blend that uses
  `X`"). 
- `--dry-run` prints the eligible blend commands without running them — **use this first** when
  unsure why a blend didn't fire.
- `--force` reruns even if the output dir exists.
- `--blend-only` / `--diagnostics-only` split the two halves of the work.

### 6.2 The configured blends

**India** (`blend/output/india2026/<date>/<name>/`):

| Blend name | Deterministic | Ensemble | Input files |
|---|---|---|---|
| `AIFS_single_v1p1_NCUM` | AIFS_single_v1p1 | NCUM | `AIFS/output/india/AIFS_single_v1p1/tp/tp_0p25_<date>.nc` + `NCUM/output/precipitation_amount/precipitation_amount_<date>.nc` |
| `AIFS_single_v1p1_NeuralGCM` | AIFS_single_v1p1 | NeuralGCM | AIFS `tp_0p25` + `NeuralGCM/output/india/tp/tp_2p0_<date>.nc` |
| `AIFS_single_v2_NeuralGCM` | AIFS_single_v2 | NeuralGCM | AIFS v2 `tp_0p25` + NGCM `tp_2p0` *(diagnostics-only; no v2 blend coefficients)* |

- India implementations: `blend/utils/india2026/AIFS_NCUM_blend/main.py` and
  `blend/utils/india2026/AIFS_NGCM_blend/main.py`. Output at the **subdistrict** level.

**Ethiopia** (`blend/output/ethiopia2026/<date>/<name>/`):

| Blend name | Deterministic | Ensemble | Notes |
|---|---|---|---|
| `AIFS_single_v1p1_AIFS_ENS_v1` | AIFS_single_v1p1 | AIFS_ENS_v1 | blend + diagnostics |
| `AIFS_single_v2_AIFS_ENS_v2` | AIFS_single_v2 | AIFS_ENS_v2 | blend + diagnostics |
| `AIFS_single_v2_NeuralGCM` | AIFS_single_v2 | NeuralGCM | blend + diagnostics |
| `AIFS_single_v2_gencast` | AIFS_single_v2 | gencast | **diagnostics-only** (`blend_implemented=False`) |

- Ethiopia implementation: `blend/utils/ethiopia2026/run_pipeline.py` (a git submodule under
  `blend/utils/ethiopia2026/operational`). It produces district-level products and supports two
  onset definitions (ICPAC and 2 mm).

> **If you re-cloned the repo and the Ethiopia blend errors with a missing module**, the submodule
> isn't checked out: `git submodule update --init --recursive`.

### 6.3 Manual blend recovery

```bash
cd /net/monsoon/operational/monsoon-onset/blend/utils
conda activate /net/scratch2/marchakitus/conda-envs/operational

# What's eligible for this date right now?
python ./main.py --date 20260604T00 --dry-run

# Run every India blend whose inputs are present:
python ./main.py --date 20260604T00 --region india

# Re-run one specific blend even though its output exists:
python ./main.py --date 20260604T00 --blend AIFS_single_v1p1_NCUM --force

# Only (re)make the diagnostic plots, not the blend:
python ./main.py --date 20260604T00 --region ethiopia --diagnostics-only
```

**Why a blend "didn't run" — read the log line.** The dispatcher prints exactly one of:
- `… is not ready for <date>. Missing: <model>=<path>` → an input file is absent (go regenerate
  that model first).
- `… already has output at <dir>; skipping. Use --force to rerun.` → it's done; add `--force` to
  redo.
- `… is disabled; no blend coefficients are configured.` → `blend_implemented=False` (gencast /
  v2 NGCM India); only diagnostics will run.

---

## 7. Diagnostics & sync

### 7.1 Model diagnostics

The blend dispatcher also drives `model_diagnostics/utils/main.py` (it builds a
`--region/--deterministic_model/--ensemble_model/--deterministic_input/--ensemble_input/--output_dir`
command per eligible blend). To force just the diagnostics for a date, use
`blend/utils/main.py … --diagnostics-only` (§6.3).

India diagnostics helpers (run from `model_diagnostics/`, env `operational`) — useful for
back-filling observed-rainfall context:

```bash
cd /net/monsoon/operational/monsoon-onset/model_diagnostics
python -m utils.india.get_subdistrict_rainfall 20260604     # observed IMD → subdistrict CSV + maps
python -m utils.india.onset_subdistrict_criteria 20260604   # onset criteria per subdistrict
python -m utils.create_matrix                               # rebuild regridding weight matrices (rare)
```

### 7.2 Sync to Google Drive — `sync/utils/main.py`

Runs every 10 min via `sync.sh` (env `operational_pip`). Default action `sync` updates live
assets then uploads configured rules to Drive; it tracks state in
`sync/state/drive_inventory_<region>_dsi.sqlite3`.

```
python ./sync/utils/main.py [sync|reconcile|ls-drive|live]
        [--region india ethiopia ...] [--date YYYYMMDDTHH ...]
        [--rule NAME ...] [--repair-mode report|upload-missing]
        [--workers N] [--dry-run] [--skip-live]
```

Common operations:

```bash
cd /net/monsoon/operational/monsoon-onset/sync/utils
conda activate /net/scratch2/marchakitus/conda-envs/operational_pip

# Re-push one date for one region (most common):
python ./main.py sync --region india --date 20260604T00

# See what would upload without doing it:
python ./main.py sync --dry-run

# Find (and optionally fix) files that exist locally but not on Drive:
python ./main.py reconcile --region india
python ./main.py reconcile --region india --repair-mode upload-missing

# List what's already on Drive for a date:
python ./main.py ls-drive --region india --date 20260604T00
```

Drive OAuth credentials live in `sync/.auth/` (`credentials.json`, auto-refreshed `token.json`).
If auth fails, delete `token.json` and re-run interactively to re-authorize. The destination
roots are `…/MO_operational_data_2026/dsi` (India) and the Ethiopia equivalents — see
`sync/config/sync.yaml` for the full rule list.

To force a re-sync of a date that the inventory thinks is already uploaded, either pass `--date`
(it re-checks) or, as a last resort, delete that date's rows from the sqlite inventory and re-run.

### 7.3 IITM sync (India AIFS tp → IITM Drive)

A separate, lightweight path (`sync_IITM/utils/main.py`, env `operational_pip`, every 10 min) that
publishes `AIFS_single_v1p1` `tp_0p25` files to the IITM Drive folder (renaming `tp_0p25_<date>.nc`
→ `tp_<date>.nc`, public-read). Manual trigger:

```bash
cd /net/monsoon/operational/monsoon-onset/sync_IITM/utils
conda activate /net/scratch2/marchakitus/conda-envs/operational_pip
python ./main.py
```

---

## 8. Debugging playbook — "a forecast is missing or failed"

Work top to bottom. Most incidents are resolved by step 2 or 3.

### Step 0 — Take inventory with `scripts/check_pipeline.py`

Before reading any logs, ask the repo what actually exists. `scripts/check_pipeline.py`
checks every expected artifact for a date — downloaded inputs, raw model output, per-region
post-processed products, blend output, and model diagnostics — across **all models and blends**,
and tells you exactly which stage is the first to come up empty. It reads the blend/diagnostics
layout straight from `blend/utils/main.py` (`BLENDS`) and the model/observation paths from the
static patterns in §5, so it stays in sync with the pipeline. Discovery is read-only; it never
deletes unless you ask it to.

```bash
conda activate /net/scratch2/marchakitus/conda-envs/operational   # any env with the repo on it

# Full inventory for one date (run from the repo root):
python ./scripts/check_pipeline.py --date 20260604T00

# Just the gaps — the fastest "where did it break?" view:
python ./scripts/check_pipeline.py --date 20260604T00 --missing-only

# Narrow to one model / region / stage when you already suspect where:
python ./scripts/check_pipeline.py --date 20260604T00 --model NeuralGCM --stage raw
python ./scripts/check_pipeline.py --date 20260604T00 --region india --stage postprocessed

# Sweep a back-fill range, or emit JSON for scripting:
python ./scripts/check_pipeline.py --start-date 20260601 --end-date 20260604 --missing-only
python ./scripts/check_pipeline.py --date 20260604T00 --json
```

Each row prints a status and the path it checked:

```
  [raw]
    [ok] AIFS_single_v2/-/raw: AIFS/output/raw/AIFS_single_v2/init_20260604T00.nc
  [postprocessed]
    [--] NeuralGCM/india/tp_0p25: NeuralGCM/output/india/tp/tp_0p25_20260604T00.nc
```

`[ok]` present · `[--]` missing but expected (a real gap — start here) · `[..]` absent and not
required (e.g. the diagnostics-only GenCast blend has no blend output). The first stage showing
`[--]` is where to focus; jump to that stage's manual command in §4.

**Granularity filters** (all repeatable, combine freely): `--stage`
{`inputs`,`raw`,`postprocessed`,`blend`,`diagnostics`}, `--model NAME` (a model or observation
group: `AIFS_single_v2`, `NeuralGCM`, `gencast`, `NCUM`, `IMERG`, `IMD`, `S2S`, `ecmwf`, `ncep`),
`--blend NAME`, `--region` {`india`,`ethiopia`}, `--label PRODUCT` (e.g. `tp_0p25`, `sji`, `raw`).
Dates come from `--date` (repeatable, accepts `YYYYMMDD[THH]`) or a `--start-date`/`--end-date`
range. Output: `--missing-only`, `--present-only`, `--json`.

**Deleting a bad stage so it can be re-run cleanly.** Add `--delete` to remove the *existing*
artifacts that match your filters. It is off by default, requires an explicit date, lists every
target and asks for confirmation first, warns loudly on shared inputs (one ECMWF/GDAS file feeds
every model for that cycle), and refuses any path outside the repo. Use `--dry-run` to preview and
`--yes` to skip the prompt in scripts.

```bash
# Preview what a delete would remove (changes nothing):
python ./scripts/check_pipeline.py --date 20260604T00 --model NeuralGCM --stage raw --delete --dry-run

# Drop just the raw NeuralGCM Zarr for one date, then re-drive from §4.4 / §3.2:
python ./scripts/check_pipeline.py --date 20260604T00 --model NeuralGCM --stage raw --delete

# Wipe everything for a hopelessly broken date (lists + confirms first):
python ./scripts/check_pipeline.py --date 20260604T00 --delete
```

After deleting, re-run the relevant stage by hand (§4) or re-drive the whole stream (§3.2 / Step 5
below). Then re-run `check_pipeline.py` to confirm the gaps are filled.

### Step 1 — Is it actually missing, or just not due yet?
- AIFS/NGCM/GenCast run on the **00Z** cycle; ECMWF/GDAS publish hours after 00Z. A 06:00-local
  gap is normal. Check the cron logs (§2) for `no new data was found` (benign).

### Step 2 — Did the cron tick run and what did it say?
```bash
tail -n 60 HPC/dsi/cron/logs/AIFS.log     # or NGCM/IMERG/S2S/NCUM/sync.log
```
- `cron already running; skipping this tick.` for many ticks → a previous run is **stuck holding
  the lock**. Find and clear it:
  ```bash
  ls -l /tmp/monsoon-*-dsi.lock
  ps aux | grep -E 'main.py|run_model' | grep -v grep
  # kill the stale process, or just remove the lock if no process holds it:
  rm -f /tmp/monsoon-aifs-dsi.lock
  ```
- `conda not found in PATH` → environment/login shell problem on the cron host.
- `ERROR Job timed out` → the orchestrator itself (not the Slurm job) exceeded its timeout, usually
  because a download stalled. Re-run the orchestrator by hand (§3.2).

### Step 3 — Was the Slurm job submitted / how did it end?
```bash
squeue -u $USER                                    # is it still running/queued?
ls -lt HPC/dsi/logs/AIFS_single_v2/ | head         # newest .o / .e files
tail -n 80 HPC/dsi/logs/AIFS_single_v2/AIFS_single_v2_20260604T00.e<jobid>
sacct -X --name=AIFS_single_v2_20260604T00 \
      --format=JobID,State,Elapsed,ExitCode,MaxRSS  # post-mortem
```
- `OUT_OF_MEMORY` / killed → memory pressure; check `MaxRSS` vs the script's `--mem`.
- `TIMEOUT` → GPU contention or a slow step; resubmit when the cluster is quieter.
- `CUDA out of memory` / `no CUDA-capable device` in the `.e` log → GPU not allocated or shared;
  confirm the `--gres` line and that you're on a GPU node.
- `FileNotFoundError` on an input GRIB/nc → the **previous** stage didn't produce its output;
  drop back to that stage's manual command (§4).

### Step 4 — Reproduce the failing step interactively
Run the exact Python command from §4 for the stage that failed, in the right env, **without**
Slurm. You'll see the full traceback immediately. This is the single most useful debugging move —
the batch scripts are just these commands in sequence.

### Step 5 — Re-drive from the failed stage
- Need to re-run the **whole** stream for a date: `python ./HPC/utils/main.py --pipelines <name>
  --date <date>` (§3.2).
- Model output exists but **blend** is missing: `blend/utils/main.py --date <date> --force` (§6.3).
- Products exist locally but aren't on Drive: `sync/utils/main.py reconcile … --repair-mode
  upload-missing` (§7.2).
- A stage left **partial or corrupt** output that blocks a re-run (a half-written Zarr, a truncated
  product): clear just that stage with `scripts/check_pipeline.py … --stage <stage> --delete`
  (Step 0), then re-drive.

---

## 9. Quick reference card

```bash
cd /net/monsoon/operational/monsoon-onset
eval "$(conda shell.bash hook)"

# ── status ──────────────────────────────────────────────────────────────
python ./scripts/check_pipeline.py --date 20260604T00 --missing-only  # what's missing? (§8 Step 0)
crontab -l | grep monsoon                         # cron installed?
tail HPC/dsi/cron/logs/{AIFS,NGCM,IMERG,S2S,NCUM,sync}.log
squeue -u $USER                                   # running jobs
ls /tmp/monsoon-*-dsi.lock                         # stuck locks

# ── re-drive a whole stream for a date ──────────────────────────────────
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./HPC/utils/main.py --pipelines ecmwf --date 20260604T00   # AIFS+ENS+GenCast
python ./HPC/utils/main.py --pipelines ngcm  --date 20260604T00
python ./HPC/utils/main.py --pipelines imerg --date 20260604
python ./HPC/utils/main.py --pipelines s2s   --date 20260604T00
python ./HPC/utils/main.py --pipelines aifs  --dry-run            # preview only

# ── single steps (env per §1.2) ─────────────────────────────────────────
python ./IC/utils/download_ecmwf.py --date 20260604T00            # env AIFS_ENS
python ./IC/utils/download_ncep.py  --date 20260604T00            # env operational
( cd AIFS/utils      && python ./run_model.py --date 20260604T00 --model AIFS_single_v2 )
( cd AIFS/utils      && python ./post_process.py --date 20260604T00 --model AIFS_single_v2 )
( cd NeuralGCM/utils && python ./preprocess_ic.py --date 20260604T00 )
( cd NeuralGCM/utils && python ./run_model.py --date 20260604T00 )
( cd NeuralGCM/utils && python ./post_process.py --date 20260604T00 && python ./post_process_merge.py --date 20260604T00 )
( cd NCUM/utils      && python ./main.py --date 20260604T00 )
( cd IMERG/utils     && python ./plot.py --date 20260604 && python ./plot_bias.py --date 20260604 )

# ── blend / diagnostics / sync ──────────────────────────────────────────
( cd blend/utils && python ./main.py --date 20260604T00 --dry-run )
( cd blend/utils && python ./main.py --date 20260604T00 --region india )
( cd blend/utils && python ./main.py --date 20260604T00 --blend AIFS_single_v1p1_NCUM --force )
( cd sync/utils  && python ./main.py sync --region india --date 20260604T00 )
( cd sync/utils  && python ./main.py reconcile --region india --repair-mode upload-missing )
```

---

## 10. Appendix

### 10.1 DSI Slurm jobs at a glance

| Script | Partition | GPUs | CPU / Mem | Wall | Models |
|---|---|---|---|---|---|
| `run_AIFS.sh` | general | 1× A100 | 8 / 64 GB | 1 h | AIFS_single_v1p1, _v2 |
| `run_AIFS_ENS.sh` | general | 4× A100 | 16 / 200 GB | 2 h | AIFS_ENS_v1, _v2 (Ethiopia) |
| `run_gencast.sh` | Monsoon | 4× H200 | 32 / 350 GB | 12 h | gencast (Ethiopia) |
| `run_NGCM.sh` | general | 4× A100 | 16 / 120 GB | 1 h | NeuralGCM (India+Ethiopia) |
| `process_IMERG.sh` | general | – | 2 / 32 GB | 30 m | IMERG/IMD (India) |
| `process_S2S.sh` | general | – | 2 / 32 GB | 30 m | S2S |

### 10.2 Models (`config/models.json`)

| Model | Weights | IC source | Regions |
|---|---|---|---|
| AIFS_single_v1p1 | aifs-single-mse-1.1.ckpt | ecmwf | india, ethiopia |
| AIFS_single_v2 | aifs-single-mse-2.0.ckpt | ecmwf (oper+wave) | ethiopia |
| AIFS_ENS_v1 | aifs-ens-crps-1.0.ckpt | ecmwf | ethiopia |
| AIFS_ENS_v2 | aifs-ens-crps-2.0.ckpt | ecmwf (oper+wave) | ethiopia |
| gencast | GenCast 0p25deg Operational <2022.npz | ecmwf (+SST) | ethiopia |
| NeuralGCM | models_v1_precip_stochastic_precip_2_8_deg.pkl | ncep | india, ethiopia |

### 10.3 Credentials / auth locations

| Service | Where |
|---|---|
| Google Drive (main sync) | `sync/.auth/credentials.json`, `sync/.auth/token.json` |
| Google Drive (IITM) | `sync_IITM/.auth/` |
| ECMWF CDS / datastores (S2S) | `~/.cdsapirc` |
| NASA Earthdata (IMERG) | `IMERG/.auth/.urs_cookies` |
| NCMRWF API (NCUM) | `NCUM/.auth/keys.json` |

### 10.4 Log locations

| Logs | Path |
|---|---|
| Cron wrapper output (per stream) | `HPC/dsi/cron/logs/{AIFS,NGCM,IMERG,S2S,NCUM,sync}.log` |
| Slurm job stdout/stderr | `HPC/dsi/logs/<LABEL>/<LABEL>_<DATE_F>.{o,e}<jobid>` |
| IITM sync | `sync_IITM/logs/cron.log`, `sync_IITM/logs/drive.txt` |

### 10.5 Notes for the maintainer

- Conda env prefixes and the cron host user (`marchakitus`) are hard-coded in the DSI scripts and
  `.config/envs.json`. If the owning account changes, update those.
- The repository tree on the operational box also holds generated outputs, raw data, logs, weights
  and credentials alongside the source — don't `git clean` it.
- This manual reflects the DSI scripts as checked in. If you change a batch script's step order or
  a product path, update §4 / §5 here too.

---
*Document scope: DSI cluster. For Midway/Derecho see `HPC/midway/` and `HPC/derecho/`. For the
cloud pipeline see `README_CLOUD.md`.*
