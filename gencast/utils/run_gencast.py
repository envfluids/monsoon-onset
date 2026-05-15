
import jax
import jax.numpy as jnp
import logging

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
jax.config.update("jax_persistent_cache_enable_xla_caches", "xla_gpu_per_fusion_autotune_cache_dir")

N_MEMBERS = 4
N_DAYS = 50
N_STEPS = N_DAYS * 2 # 2 steps per day (12h interval)

MODEL_PATH = BASE / "weights" / "GenCast 0p25deg Operational <2022.npz"
STATS_DIR = BASE / "data"
FCST_DIR = BASE / "raw" / "output"


def get_model():
    with open(MODEL_PATH, "rb") as f:
        ckpt = checkpoint.load(f, gencast.CheckPoint)
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


def run_model(date_f):
    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
    outfile = FCST_DIR / f"init_{date_f}.nc"
    if outfile.exists():
        logging.info(f"Output file {outfile} already exists. Skipping run.")
        return
    run_forward_pmap, task_config = get_model()
    ic = import_ic(task_config, date)
    lat = ic.lat.values
    lon = ic.lon.values
    level = ic.level.values
    forcings_ds, targets_template_ds = get_forcings_targets(task_config, date, N_STEPS, lat, lon, level)
    num_ensemble_members = N_MEMBERS   # @param int
    rng = jax.random.PRNGKey(0)

    rngs = np.stack(
        [jax.random.fold_in(rng, i) for i in range(num_ensemble_members)], axis=0)

    num_steps_per_chunk = 1
    total_steps = N_STEPS
    current_step = 0

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
        pmap_devices=jax.local_devices()
        ):
        chunks.append(chunk)
        current_step += num_steps_per_chunk
        logging.info(f"Completed {current_step}/{total_steps} steps.")
    

    predictions = xr.combine_by_coords(chunks)

    predictions.to_netcdf(outfile)




def main():
    parser = argparse.ArgumentParser(description='Run GenCast for a given date.')
    parser.add_argument('--date', type=str, help='Date to run the model for (YYYYMMDDTHH format)', required=True)
    args = parser.parse_args()
    date_f = args.date

    run_model(date_f)

if __name__ == "__main__":
    main()