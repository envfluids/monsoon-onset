## Monsoon Onset Prediction System Documentation

This document details the architecture, components, and operational workflow of the data-driven monsoon onset prediction system housed in the `monsoon-onset-adam-dev` repository. The system integrates two distinct forecasting models (AIFS and NeuralGCM), blends their outputs, generates visualizations, and synchronizes the results for operational use.

### 1. Overall System Architecture

The system is composed of four primary components orchestrated via cron jobs and shell scripts:

1.  **AIFS (ECMWF Integrated Forecasting System Interface):** Downloads ECMWF IFS data, runs the AIFS inference model, post-processes the output, and verifies completion.
2.  **NeuralGCM:** Downloads NCEP GDAS data, preprocesses it (including NCL-based interpolation), runs an ensemble forecast using the NeuralGCM model, post-processes the ensemble output (including CDO-based regridding), merges results, and verifies completion.
3.  **Blend:** Triggered upon successful completion of *both* AIFS and NeuralGCM for a given forecast initialization time. It loads the processed outputs from both models, aggregates data weekly, applies a multinomial logistic regression blending model, and generates probabilistic forecasts and map visualizations.
4.  **Sync:** Periodically checks for new blended outputs, updates a separate operational repository for the live website, and archives results to Google Drive.

**Workflow Scheduling:**

The system's operation is driven by cron jobs defined in `cron.txt`:

* `*/5 * * * *`: Runs the `sync` process every 5 minutes via `sync/utils/cron_job.sh`.
* `*/15 * * * *`: Runs the AIFS pipeline every 15 minutes via `AIFS/chron_job.sh`.
* `*/30 * * * *`: Runs the NeuralGCM pipeline every 30 minutes via `NeuralGCM/chron_job.sh`.

Each `chron_job.sh` script activates the appropriate Conda environment and executes the respective `pipeline.py` script with a timeout. The pipelines check for new input data before submitting the main model execution script (`run_model.sh`) to a batch scheduling system (SLURM, indicated by `sbatch`).

```mermaid
graph TD
    subgraph Cron Scheduling
        CronAIFS(*/15 * * * *) --> AIFS_cron[AIFS/chron_job.sh]
        CronNGCM(*/30 * * * *) --> NGCM_cron[NeuralGCM/chron_job.sh]
        CronSync(*/5 * * * *) --> Sync_cron[sync/utils/cron_job.sh]
    end

    subgraph AIFS Pipeline
        AIFS_cron --> AIFS_pipeline[AIFS/utils/pipeline.py]
        AIFS_pipeline -- Checks New Data --> AIFS_download[AIFS/utils/download_ic.py]
        AIFS_download -- If New --> AIFS_sbatch(sbatch AIFS/utils/run_model.sh)
        AIFS_sbatch --> AIFS_run[AIFS/utils/run_model.py]
        AIFS_run --> AIFS_post[AIFS/utils/post_process.py]
        AIFS_post --> Verify1[AIFS/utils/verify_completion.py]
    end

    subgraph NeuralGCM Pipeline
        NGCM_cron --> NGCM_pipeline[NeuralGCM/utils/pipeline.py]
        NGCM_pipeline -- Checks New Data --> NGCM_download[NeuralGCM/utils/download_ncep.py]
        NGCM_download -- If New --> NGCM_sbatch(sbatch NeuralGCM/utils/run_model.sh)
        NGCM_sbatch --> NGCM_pre[NeuralGCM/utils/preprocess.py]
        NGCM_pre --> NGCM_run[NeuralGCM/utils/run_model.py - Ensemble]
        NGCM_run --> NGCM_post[NeuralGCM/utils/post_process.py]
        NGCM_post --> NGCM_merge[NeuralGCM/utils/post_process_merge.py]
        NGCM_merge --> Verify2[NeuralGCM/utils/verify_completion.py]
    end

     subgraph Blend Process
        Verify1 -- Both Succeed --> Blend_main[blend/utils/main.py]
        Verify2 -- Both Succeed --> Blend_main
        Blend_main --> Blend_proc_aifs[blend/utils/aifs.py]
        Blend_main --> Blend_proc_ngcm[blend/utils/ngcm.py]
        Blend_main --> Blend_blend[blend/utils/blend.py]
        Blend_main --> Blend_maps[blend/utils/maps.py]
        Blend_main --> Blend_output(Save to sync/latest/{date})
     end

    subgraph Sync Process
        Sync_cron --> Sync_main[sync/utils/main.py]
        Sync_main -- Checks --> Blend_output
        Sync_main -- If Newer --> Update_Live_Repo(Update monsoon-operational Repo)
        Sync_main -- If Not Synced --> Sync_drive[sync/utils/drive.py]
        Sync_drive --> Archive_Drive(Archive to Google Drive)
    end

 classDef cron fill:#f9f,stroke:#333,stroke-width:2px;
 classDef script fill:#ccf,stroke:#333,stroke-width:2px;
 classDef data fill:#cfc,stroke:#333,stroke-width:2px;
 classDef process fill:#ff9,stroke:#333,stroke-width:2px;

 class CronAIFS,CronNGCM,CronSync cron;
 class AIFS_cron,NGCM_cron,Sync_cron,AIFS_sbatch,NGCM_sbatch,AIFS_run,AIFS_post,Verify1,NGCM_pre,NGCM_run,NGCM_post,NGCM_merge,Verify2,Blend_main,Blend_proc_aifs,Blend_proc_ngcm,Blend_blend,Blend_maps,Sync_main,Sync_drive script;
 class AIFS_pipeline,NGCM_pipeline script;
 class AIFS_download,NGCM_download script;
 class Blend_output,Archive_Drive,Update_Live_Repo data;
 class Verify1,Verify2 process;
```

*(Note: Mermaid diagram above describes the workflow visually)*

### 2. Component Details

#### 2.1. AIFS Component

* **Purpose:** To generate forecasts using the AIFS model based on the latest ECMWF IFS open data initial conditions.
* **Workflow:**
    1.  **Check/Download IC (`download_ic.py`):** Uses `ecmwf.opendata.Client` to find the latest available IFS cycle time. Compares this to the timestamp of the latest locally stored initial condition (`.pkl` file). If newer data is available, downloads required surface (`PARAM_SFC`), soil (`PARAM_SOIL`), and pressure level (`PARAM_PL`) variables using `earthkit.data`. Interpolates the data from its native grid (likely 0.25-degree lat/lon shifted) to the model's required grid (N320 Gaussian?) using a pre-calculated sparse matrix (`TFM_LATLON_N320`) loaded from an `.npz` file. Structures the data (including geopotential height to geopotential conversion) and saves it as a pickled dictionary (`input_state_{date}.pkl`).
    2.  **Run Model (`run_model.py`):** Initiated by `run_model.sh`. Loads the AIFS model checkpoint (`.ckpt`). Loads the corresponding `input_state_{date}.pkl`. Uses `anemoi.inference.runners.simple.SimpleRunner` to perform the forecast run for a specified `lead_time` (41 days). Iterates through the forecast steps (6-hourly), interpolates selected output fields (`2t`, `u_850`, `v_850`, `tp`, `tcw`) to a lat/lon grid using another sparse matrix (`TFM_N320_LATLON`), and aggregates these into a single NetCDF file (`init_{date}.nc`) with dimensions `(time, step, lat, lon)`.
    3.  **Post-process (`post_process.py`):** Loads the `init_{date}.nc` file. Calculates the Somali Jet Index (SJI) by averaging wind kinetic energy at 850 hPa over a defined region (`lat=slice(20.0, -5.0), lon=slice(50.0, 70.0)`). Processes Total Column Water Vapor (TCW) and Total Precipitation (TP). Regrids TCW and TP fields to a 2-degree grid (`grids/grid_2p0.txt`) using `cdo remapcon`. Aggregates TCW to daily means and TP to daily sums. Saves the results into separate NetCDF files: `sji_{date}.nc`, `tcw_{date}.nc`, `tp_{date}.nc`.
    4.  **Verify (`verify_completion.py`):** Checks if its own output files (`sji`, `tcw`, `tp`) *and* the corresponding NeuralGCM output files exist for the given `date`. If all six files are present, it exits with status 0, allowing the blend process to proceed. Otherwise, exits with status 1.
* **Key Scripts:** `pipeline.py`, `download_ic.py`, `run_model.py`, `post_process.py`, `verify_completion.py`, `run_model.sh`.
* **Inputs:** ECMWF IFS Open Data, model checkpoint (`.ckpt`), interpolation matrices (`.npz`), grid definition (`grids/grid_2p0.txt`).
* **Outputs:** Raw model output (`init_{date}.nc`), processed SJI, TCW, TP files (`{variable}_{date}.nc`).
* **Dependencies:** Conda environments (`AIFSv1`, `ncl_stable`), Python libraries (`earthkit.data`, `anemoi.inference`, `xarray`, `scipy`, `numpy`, `netCDF4`, `pickle`), System tools (`cdo`).

#### 2.2. NeuralGCM Component

* **Purpose:** To generate ensemble forecasts using the NeuralGCM model based on NCEP GDAS initial conditions.
* **Workflow:**
    1.  **Check/Download IC (`download_ncep.py`):** Queries the NOAA NOMADS server for the latest available NCEP GDAS analysis cycle (0.25-degree GRIB2 format, `pgrb2.0p25.f000`). Compares the latest available cycle time with locally downloaded files. If a newer file is needed, downloads `gdas.t{cycle}z.pgrb2.0p25.f000` and saves it as `gdas_{date}.pgrb2`.
    2.  **Preprocess (`preprocess.py`):** Executes an NCL script embedded within the Python code using `subprocess.run`. This NCL script reads the downloaded GRIB2 file, performs vertical interpolation (`int2p_n`) of key variables (Temperature, Specific Humidity, U/V winds, Geopotential Height, Cloud Water Mixing Ratio) to standard pressure levels (`new_p`), and saves the result as an intermediate NetCDF file. The Python portion then loads this intermediate NetCDF and a reference ERA5 dataset (`ERA5_2018_05_16_00.nc`) using `xarray`. It renames dimensions and variables to match expected conventions (e.g., `T_interp` -> `temperature`), assigns standard coordinates (level, latitude, longitude) from the reference dataset, converts geopotential height to geopotential, calculates specific cloud liquid and ice water content based on temperature, and saves the final processed initial conditions as `gdas_{date}.nc`.
    3.  **Run Model (`run_model.py`):** Initiated by `run_model.sh` across 4 MPI ranks/GPUs. Loads the NeuralGCM model checkpoint (`.pkl`). Loads the preprocessed initial conditions (`gdas_{date}.nc`), regrids to the model's horizontal grid using `dinosaur.horizontal_interpolation.ConservativeRegridder`, and fills NaN values. Loads climatological forcings (SST/SeaIce). Distributes the `N_MEMBERS` (30) ensemble members across the 4 MPI ranks. For each assigned member: initializes the model state using `model.encode` with a unique random key (`jax.random.key`). Runs the forecast for 45 days using `model.unroll`, saving state every 6 hours. Extracts specific variables (specific humidity on selected levels, geopotential on lower levels, U/V at 850 hPa, cumulative precipitation). Saves the output for each member as a separate Zarr dataset (`../raw/output/{date}/member_{rand}.zarr`).
    4.  **Post-process (`post_process.py`):** Processes each member's Zarr output in parallel using `concurrent.futures.ProcessPoolExecutor`. Calculates TCWV by vertically integrating specific humidity. Regrids TCWV and TP (precipitation) to the 2-degree grid (`grids/grid_2p0.txt`) using `cdo remapcon`. Aggregates TCWV to daily means and TP (converted from cumulative mean to daily totals) to daily sums. Saves these processed daily fields as intermediate NetCDF files for each member (`../output/{tcw|tp}/{member}_{date}_INTERMEDIATE_3.nc`).
    5.  **Merge (`post_process_merge.py`):** Calculates SJI (ensemble mean) directly from the raw Zarr outputs (U/V 850hPa). Opens all intermediate member TCW files using `xr.open_mfdataset` and saves the merged ensemble dataset as `../output/tcw/tcw_{date}.nc`. Does the same for TP files, saving `../output/tp/tp_{date}.nc`. Saves the calculated SJI as `../output/sji/sji_{date}.nc`. Deletes the intermediate member files.
    6.  **Verify (`verify_completion.py`):** Identical function to the AIFS version; checks for the six processed output files (`sji`, `tcw`, `tp` from both AIFS and NeuralGCM) for the given date.
* **Key Scripts:** `pipeline.py`, `download_ncep.py`, `preprocess.py`, `run_model.py`, `post_process.py`, `post_process_merge.py`, `verify_completion.py`, `run_model.sh`.
* **Inputs:** NCEP GDAS GRIB2 data, model checkpoint (`.pkl`), climatological forcings, reference ERA5 file, grid definition (`grids/grid_2p0.txt`).
* **Outputs:** Raw ensemble member outputs (`member_{rand}.zarr`), merged ensemble SJI, TCW, TP files (`{variable}_{date}.nc`).
* **Dependencies:** Conda environments (`neuralgcm`, `ncl_stable`), Python libraries (`jax`, `dinosaur`, `neuralgcm`, `xarray`, `numpy`, `netCDF4`, `pickle`, `requests`, `tqdm`), System tools (`ncl`, `cdo`, `bash`, `sbatch`).

#### 2.3. Blend Component

* **Purpose:** To combine the processed outputs from AIFS and NeuralGCM using a statistical blending model and generate final forecast products and visualizations.
* **Workflow:**
    1.  **Load Data (`main.py`, `aifs.py`, `ngcm.py`):** Triggered by `run_model.sh` from either AIFS or NeuralGCM *after* `verify_completion.py` succeeds. Loads the processed `tp_{date}.nc` files from both models. Loads auxiliary data: thresholds (`thresholds_df.csv`, `onset_five_day_thres_2deg.mat`), cell cluster information (`onset_clusters.csv`), allowed cells (`allowed_cells.csv`), and pre-computed climatological probabilities (`ensemble_outputs_clim_2025.csv`). The `aifs.py` and `ngcm.py` scripts process the respective TP files: they iterate through allowed grid cells, calculate daily onset probability (`find_onset` from `utils.py`) and quasi-onset probability (`compute_quasi_onset` from `utils.py`) based on a 5-day rolling precipitation sum exceeding a cell-specific threshold loaded from the `.mat` file. `ngcm.py` performs this calculation across the ensemble members and returns probabilities/means/standard deviations.
    2.  **Aggregate & Prepare (`main.py`):** Merges the processed DataFrames from `aifs.py` and `ngcm.py`. Calculates 5-day (left-aligned) rolling sums of daily precipitation for both models using `compute_roll_sum` from `utils.py`. Bins the forecast days into weekly intervals (`week1`, `week2`, `week3`, `week4`, `later`). Aggregates the data by `(time, lat, lon, interval)`, taking the sum of daily rain, max of 5-day totals, min of 10-day totals (Note: 10-day calculation seems intended but might use 5-day var?), and sum of climatology probabilities. Pivots the aggregated data into a wide format where columns represent variable-week combinations (e.g., `ngcm_rain_daily_week1`, `max_aifs_5day_week2`). Renames columns for clarity (e.g., `prob_clim_mr_week1`). Merges with cluster and threshold information. Applies transformations (logit) to the climatology probability columns. Saves this final pre-blend dataset as `all_data.csv`.
    3.  **Blend Probabilities (`blend.py`):** Loads the `all_data.csv`. Loads coefficients for two multinomial logistic regression models (one for the forecast, one for climatology) from `multinom_coefs_full.csv` and `multinom_coefs_full_clim.csv`. Constructs the design matrix from `all_data.csv`, including necessary interaction terms specified in the coefficient files. Calculates the softmax probabilities for each forecast category (`week1`...`later`) using both sets of coefficients. Combines the input features and the calculated probabilities (forecast and climatology) into a summary DataFrame and saves it as `blend_output_summary.csv`. Also saves a more detailed version as `blend_output_with_clim.csv`.
    4.  **Generate Maps (`maps.py`):** Loads the `blend_output_summary.csv`. Creates forecast probability maps for individual weeks (1-4) and combined periods (1-2, 3-4). Cells are colored based on forecast probability, and outlines are added (red/green) if the forecast probability deviates significantly (>=10%) from the corresponding climatology probability. Creates a bar chart map visualizing the probability distribution across weeks 1-4 and 'later' for each cell. The `make_extra_maps` function generates additional, more detailed visualizations including maps focused on the most likely onset period and individual bar plots for specific cells. Saves all maps as PNG images in a `maps` subdirectory.
    5.  **Stage Output (`main.py`):** Copies the entire output directory (`blend/output/{date}`) containing `all_data.csv`, `blend_output*.csv`, and the `maps` subdirectory to `sync/latest/`.
* **Key Scripts:** `main.py`, `aifs.py`, `ngcm.py`, `utils.py`, `blend.py`, `maps.py`.
* **Inputs:** Processed AIFS/NGCM outputs (`tp_{date}.nc`, potentially `sji`/`tcw` if fully implemented), threshold files (`.csv`, `.mat`), cluster file (`.csv`), allowed cells (`.csv`), climatology (`.csv`), blend model coefficients (`.csv`), India shapefile.
* **Outputs:** Pre-blend data (`all_data.csv`), blended probabilities (`blend_output*.csv`), map images (`maps/*.png`), staged output in `sync/latest/{date}`.
* **Dependencies:** Conda environment (likely `ncl_stable` or a dedicated blend environment), Python libraries (`pandas`, `numpy`, `xarray`, `netCDF4`, `scipy`, `matplotlib`, `geopandas`, `shapely`, `pathlib`).

#### 2.4. Sync Component

* **Purpose:** To synchronize the latest blended forecast outputs with an operational repository and Google Drive.
* **Workflow:**
    1.  **Check Latest (`main.py`):** Identifies the most recent forecast date available in the `sync/latest/` directory.
    2.  **Update Live Repo (`main.py`):** Performs a `git pull` on a separate repository (`../monsoon-operational`). Reads the date currently live (`docs/assets/data/latest.txt`). If the date found in `sync/latest/` is newer, it removes old content from the live repo's map and data directories (`docs/assets/images/`, `docs/assets/data/`), copies the new `map_bars.png` and `blend_output_summary.csv` from `sync/latest/{date}/`, updates `latest.txt` and `cluster.txt` (with hostname), and performs `git add .`, `git commit`, and `git push` to update the live repository.
    3.  **Sync to Drive (`main.py`, `drive.py`):** Checks a log file (`sync/logs/drive.txt`) to see if the latest date has already been synced. If not, it calls `drive_sync`. The `drive.py` script handles Google Drive authentication (using OAuth2 with `credentials.json`, `token.json` stored in `sync/.auth/`), finds or creates the correct folder structure on Drive (`/MO Forecast Benchmarking/operational_data/{cluster}/{date}`), and uploads the AIFS output files (`sji`, `tcw`, `tp`), NeuralGCM output files (`sji`, `tcw`, `tp`), and the entire blended output directory (`blend/output/{date}/`) recursively, skipping files that already exist on Drive. After successful sync, `main.py` appends the date to `sync/logs/drive.txt`.
* **Key Scripts:** `main.py`, `drive.py`, `cron_job.sh`.
* **Inputs:** Blended output in `sync/latest/{date}`, operational git repository state, Google Drive credentials (`credentials.json`, `token.json`), sync log (`drive.txt`).
* **Outputs:** Updated operational git repository, archived data on Google Drive.
* **Dependencies:** Conda environment (`monsoon`), Python libraries (`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`, `pathlib`), System tools (`git`, `bash`).

### 3. Environment and Dependencies

* **Conda Environments:** The system relies on multiple Conda environments:
    * `AIFSv1`: For running AIFS inference.
    * `neuralgcm`: For running NeuralGCM inference and parts of its post-processing.
    * `ncl_stable`: For running NCL preprocessing (NeuralGCM) and CDO-based post-processing (both models).
    * `monsoon`: For running the sync process.
* **System Tools:**
    * `ncl`: Required for NeuralGCM preprocessing.
    * `cdo`: Required for AIFS and NeuralGCM post-processing (regridding).
    * `git`: Required for the sync process.
    * `bash`, `sbatch`: For script execution and job submission.
* **Key Python Libraries:** `xarray`, `numpy`, `pandas`, `netCDF4`, `scipy`, `requests`, `tqdm`, `earthkit.data`, `ecmwf.opendata`, `anemoi.inference`, `jax`, `dinosaur`, `neuralgcm`, `matplotlib`, `geopandas`, `shapely`, `google-api-python-client`, `google-auth-oauthlib`.
* **Other Data:** Pre-trained model checkpoints (`.ckpt`, `.pkl`), interpolation/regridding files (`.npz`, `grid_2p0.txt`), auxiliary data for blending (`.csv`, `.mat`), shapefiles.

### 4. Operational Notes

* **Logging:** Each component utilizes Python's `logging` module. Logs for batch jobs (`sbatch`) are directed to files specified in the `run_model.sh` scripts (e.g., `../logs/AIFS_fc_{DATE_F}.o%j`). Cron job outputs are redirected to log files (e.g., `../logs/cron.log`). The Google Drive sync script also logs its progress.
* **Failure Points:**
    * Data download failures (server issues, network problems).
    * Preprocessing failures (NCL/CDO errors, file not found).
    * Model inference errors (resource limits, numerical instability, CUDA issues).
    * Post-processing errors (missing files, regridding issues).
    * Verification failures (one model finishes much later than the other).
    * Blending errors (missing input data, coefficient issues).
    * Sync failures (git errors, Google Drive API errors, authentication issues).
    * Timeouts specified in `chron_job.sh` scripts.
* **Configuration:** Key parameters like file paths, model names, lead times, ensemble members (`N_MEMBERS`), selected variables, grid definitions, and Google Drive paths are often hardcoded within the scripts. Thresholds and blending coefficients are externalized to data files.

This documentation provides a foundational understanding of the monsoon onset prediction system. Further details can be found by examining the specific scripts referenced.
