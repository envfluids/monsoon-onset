
import jax
import jax.numpy as jnp
import json
import logging
import os

import haiku as hk
import numpy as np
from preprocess_ic import get_ic

from graphcast import rollout
from graphcast import xarray_jax
from graphcast import normalization
from graphcast import checkpoint
from graphcast import data_utils
from graphcast import gencast
from graphcast import nan_cleaning

import h5netcdf
import xarray as xr
import pandas as pd
from pathlib import Path
import argparse
import datetime


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE = Path(__file__).parent.parent

REPO_ROOT = BASE.parent

JAX_CACHE_DIR = REPO_ROOT.parent / "jax_cache"
JAX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
jax.config.update("jax_compilation_cache_dir", str(JAX_CACHE_DIR))
jax.config.update("jax_persistent_cache_min_entry_size_bytes", -1)
jax.config.update("jax_persistent_cache_min_compile_time_secs", 0)

N_MEMBERS = 24
N_DAYS = 50
N_STEPS = N_DAYS * 2 # 2 steps per day (12h interval)

MODEL_PATH = BASE / "weights" / "GenCast 0p25deg Operational <2022.npz"
STATS_DIR = BASE / "data"
FCST_DIR = BASE / "raw" / "output"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def initialize_jax_runtime() -> dict[str, object]:
    batch_task_index = os.getenv("BATCH_TASK_INDEX")
    batch_task_count = os.getenv("BATCH_TASK_COUNT")
    batch_job_id = os.getenv("BATCH_JOB_ID")

    if batch_task_index is not None and batch_job_id is not None:
        logging.info(
            "Detected Cloud Batch environment: task_index=%s, task_count=%s, job_id=%s",
            batch_task_index,
            batch_task_count,
            batch_job_id,
        )
        # Cloud Batch internal DNS: <job-id>-0-<task-index>
        coordinator_address = f"{batch_job_id}-0-0:1234"
        jax.distributed.initialize(
            coordinator_address=coordinator_address,
            num_processes=int(batch_task_count or 1),
            process_id=int(batch_task_index),
        )
    elif _env_bool("GENCAST_JAX_DISTRIBUTED"):
        logging.info("Initializing JAX distributed runtime (auto-detect).")
        jax.distributed.initialize()

    local_devices = jax.local_devices()
    global_devices = jax.devices()
    runtime = {
        "jax_version": jax.__version__,
        "backend": jax.default_backend(),
        "process_index": jax.process_index(),
        "process_count": jax.process_count(),
        "local_device_count": jax.local_device_count(),
        "global_device_count": jax.device_count(),
        "local_devices": [str(device) for device in local_devices],
        "global_devices": [str(device) for device in global_devices],
    }
    logging.info("GenCast JAX runtime: %s", json.dumps(runtime, sort_keys=True))

    expected_global = os.getenv("GENCAST_EXPECTED_GLOBAL_DEVICES")
    if expected_global is not None and jax.device_count() != int(expected_global):
        raise RuntimeError(
            f"Expected {expected_global} global JAX devices, found {jax.device_count()}"
        )

    expected_local = os.getenv("GENCAST_EXPECTED_LOCAL_DEVICES")
    if expected_local is not None and jax.local_device_count() != int(expected_local):
        raise RuntimeError(
            f"Expected {expected_local} local JAX devices, found {jax.local_device_count()}"
        )

    expected_processes = os.getenv("GENCAST_EXPECTED_PROCESS_COUNT")
    if expected_processes is not None and jax.process_count() != int(expected_processes):
        raise RuntimeError(
            f"Expected {expected_processes} JAX processes, found {jax.process_count()}"
        )

    return runtime


def write_run_metadata(date_f: str, runtime: dict[str, object]) -> None:
    FCST_DIR.mkdir(parents=True, exist_ok=True)
    metadata_path = FCST_DIR / f"run_metadata_{date_f}.json"
    with metadata_path.open("w") as f:
        json.dump(runtime, f, indent=2, sort_keys=True)


def get_model():
    with open(MODEL_PATH, "rb") as f:
        ckpt = checkpoint.load(f, gencast.CheckPoint)
        if not _env_bool("GENCAST_JAX_DISTRIBUTED"):
            logging.info("Running in non-distributed mode, modifying model config for GPU execution.")
            denoiser_architecture_config = ckpt.denoiser_architecture_config
            denoiser_architecture_config.sparse_transformer_config.attention_type = "triblockdiag_mha"
            denoiser_architecture_config.sparse_transformer_config.mask_type = "full"
    params = ckpt.params
    state = {}

    task_config = ckpt.task_config
    sampler_config = ckpt.sampler_config
    noise_config = ckpt.noise_config
    noise_encoder_config = ckpt.noise_encoder_config
    denoiser_architecture_config = ckpt.denoiser_architecture_config
    print("Model description:\n", ckpt.description, "\n")
    print("Model license:\n", ckpt.license, "\n")

    diffs_stddev_by_level = xr.open_dataset(STATS_DIR / "diffs_stddev_by_level.nc").compute()
    mean_by_level = xr.open_dataset(STATS_DIR / "mean_by_level.nc").compute()
    stddev_by_level = xr.open_dataset(STATS_DIR / "stddev_by_level.nc").compute()
    min_by_level = xr.open_dataset(STATS_DIR / "min_by_level.nc").compute()

    def construct_wrapped_gencast():
        """Constructs and wraps the GenCast Predictor."""
        predictor = gencast.GenCast(
            sampler_config=sampler_config,
            task_config=task_config,
            denoiser_architecture_config=denoiser_architecture_config,
            noise_config=noise_config,
            noise_encoder_config=noise_encoder_config,
        )

        predictor = normalization.InputsAndResiduals(
            predictor,
            diffs_stddev_by_level=diffs_stddev_by_level,
            mean_by_level=mean_by_level,
            stddev_by_level=stddev_by_level,
        )

        predictor = nan_cleaning.NaNCleaner(
            predictor=predictor,
            reintroduce_nans=True,
            fill_value=min_by_level,
            var_to_clean='sea_surface_temperature',
        )

        return predictor


    @hk.transform_with_state
    def run_forward(inputs, targets_template, forcings):
        predictor = construct_wrapped_gencast()
        return predictor(inputs, targets_template=targets_template, forcings=forcings)


    run_forward_jitted = jax.jit(
        lambda rng, i, t, f: run_forward.apply(params, state, rng, i, t, f)[0]
    )
    # We also produce a pmapped version for running in parallel.
    run_forward_pmap = xarray_jax.pmap(run_forward_jitted, dim="sample")

    return run_forward_pmap, task_config


def get_forcings_targets(task_config, date, lt, lat, lon, level):
    dt = 12
    times = [pd.Timedelta(dt*i+dt, 'h') for i in range(lt)]
    datetimes = [date+t for t in times]

    forcings = xr.Dataset(
                    coords={
                        "datetime" : datetimes,
                        "time" : times,
                        "lat": lat,
                        "lon": lon,})

    data_utils.add_derived_vars(forcings)
    forcings = forcings.drop_vars(["datetime", "year_progress", "day_progress"])
    forcings = forcings.drop_vars("lat")
    forcings = forcings.expand_dims("batch", axis=0)
    
    forcings = forcings.transpose("batch", "time", "lon")
    
    target_vars = list(task_config.target_variables)
    target_vars_sfc = [
        '2m_temperature',
        'mean_sea_level_pressure',
        '10m_v_component_of_wind',
        '10m_u_component_of_wind',
        'total_precipitation_12hr',
        'sea_surface_temperature',
    ]

    data_vars = {}
    for var in target_vars:
        if var in target_vars_sfc:
            # Surface variables (no level dimension)
            data_vars[var] = (("time", "lat", "lon"),
                              np.zeros((len(times),
                              lat.size,
                              lon.size), dtype=np.float32))
        else:
            # Variables with level dimension
            data_vars[var] = (("time", "level", "lat", "lon"),
                              np.zeros((len(times),
                              level.size,
                              lat.size,
                              lon.size), dtype=np.float32))

    targets_template = xr.Dataset(
        data_vars=data_vars,
        coords={
            "time": times,
            "lat": lat,
            "lon": lon,
            "level": level
        }
    )

    targets_template = targets_template.expand_dims("batch")
    return forcings, targets_template

def import_ic(task_config, date):
    ic = get_ic(date)
    if set(task_config.forcing_variables) & data_utils._DERIVED_VARS:
        data_utils.add_derived_vars(ic)
    if set(task_config.forcing_variables) & {data_utils.TISR}:
        data_utils.add_tisr_var(ic)
    
    ic['time'] = [-pd.Timedelta('12h'), pd.Timedelta(0)]

    ic = ic.drop_vars(["datetime", "year_progress", "day_progress"])

    return ic


def run_model(date_f, runtime):
    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
    outfile = FCST_DIR / f"init_{date_f}.nc"
    if outfile.exists():
        logging.info(f"Output file {outfile} already exists. Skipping run.")
        write_run_metadata(date_f, runtime)
        return
    run_forward_pmap, task_config = get_model()
    ic = import_ic(task_config, date)
    lat = ic.lat.values
    lon = ic.lon.values
    level = ic.level.values
    forcings_ds, targets_template_ds = get_forcings_targets(task_config, date, N_STEPS, lat, lon, level)
    num_ensemble_members = int(os.getenv("GENCAST_ENSEMBLE_MEMBERS", str(N_MEMBERS)))
    rng = jax.random.PRNGKey(0)

    rngs = np.stack(
        [jax.random.fold_in(rng, i) for i in range(num_ensemble_members)], axis=0)

    num_steps_per_chunk = 1
    total_steps = N_STEPS
    current_step = 0
    pmap_devices = jax.devices() if _env_bool("GENCAST_JAX_DISTRIBUTED") else jax.local_devices()
    if num_ensemble_members != len(pmap_devices):
        raise RuntimeError(
            f"GenCast ensemble members ({num_ensemble_members}) must match pmap devices "
            f"({len(pmap_devices)}) for this execution."
        )
    logging.info(
        "Running GenCast with %s ensemble members over %s pmap devices.",
        num_ensemble_members,
        len(pmap_devices),
    )

    chunks = []
    for chunk in rollout.chunked_prediction_generator_multiple_runs(
        # Use pmapped version to parallelise across devices.
        predictor_fn=run_forward_pmap,
        rngs=rngs,
        inputs=ic,
        targets_template=targets_template_ds * np.nan,
        forcings=forcings_ds,
        num_steps_per_chunk = 1,
        num_samples = num_ensemble_members,
        pmap_devices=pmap_devices
        ):
        chunks.append(chunk)
        current_step += num_steps_per_chunk
        logging.info(f"Completed {current_step}/{total_steps} steps.")
    

    predictions = xr.combine_by_coords(chunks)

    if jax.process_index() == 0:
        predictions.to_netcdf(outfile)
        logging.info("Wrote GenCast output to %s", outfile)
    else:
        logging.info("Skipping NetCDF write on nonzero JAX process %s.", jax.process_index())
    write_run_metadata(date_f, runtime)




def main():
    parser = argparse.ArgumentParser(description='Run GenCast for a given date.')
    parser.add_argument('--date', type=str, help='Date to run the model for (YYYYMMDDTHH format)', required=True)
    args = parser.parse_args()
    date_f = args.date

    runtime = initialize_jax_runtime()
    run_model(date_f, runtime)

if __name__ == "__main__":
    main()
