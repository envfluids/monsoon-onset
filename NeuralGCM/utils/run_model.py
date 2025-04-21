import jax
import numpy as np
import pickle
import xarray
import pandas as pd

from dinosaur import horizontal_interpolation
from dinosaur import spherical_harmonic
from dinosaur import xarray_utils
import neuralgcm
import os
import argparse
import time
from datetime import datetime
import logging

import warnings
warnings.filterwarnings("ignore", message="Consolidated metadata is currently not part.*")

output_path = "../raw/output"
N_MEMBERS = 30

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
# output_path = '/glade/derecho/scratch/mgupta/NCGM_Tutorial_Hamid/ncgm_monsoon_2.8_precip_S2S_clm'

# Pre-trained NeuralGCM models can be loaded from checkpoint files saved to the disk with the from_checkpoint() constructor.
model_name = 'models_v1_precip_stochastic_precip_2_8_deg.pkl'
with open(f'../weights/{model_name}', 'rb') as f:
    ckpt = pickle.load(f)

def get_forcings_clim(year):
    forcings_clim = xarray.open_dataset('../data/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc')

    # Create a new time coordinate
    new_time = pd.date_range(str(year) +"-01-01T00:00:00.000000000", str(year) +"-12-31T00:00:00.000000000", freq="D")  # Leap year with 366 days

    # # Remove February 29 (leap day)
    if new_time.shape[0] > 365:
        new_time = new_time[new_time.dayofyear != 60]  # Exclude day 60, which corresponds to Feb 29

    # Assign the new time coordinate
    forcings_clim = forcings_clim.assign_coords(time=("time", new_time))
    return forcings_clim

# Fixing the global mean of log surface pressure for stability.
new_model_config_str = '\n'.join([
    ckpt['model_config_str'],
    # Adding FixGlobalMeanFilter which fixes global mean log surface pressure.
    (
        'dycore/SequentialStepFilter.filter_modules ='
        ' (@dycore/ExponentialFilter,@stability/ExponentialFilter,@surface_pressure/FixGlobalMeanFilter)'
    ),
])
ckpt['model_config_str'] = new_model_config_str

# trained models are encapsulated in a neuralgcm.PressureLevelModel object.
model = neuralgcm.PressureLevelModel.from_checkpoint(ckpt)

# ERA5 data on google cloud: Analysis-Ready & Cloud Optimized (ARCO) ERA5 datasets.
era5_path = '../data/model_ds/ERA5_2018_05_16_00.nc'
full_era5 = xarray.open_dataset(era5_path)
era5_grid = spherical_harmonic.Grid(
        latitude_nodes=full_era5.sizes['latitude'],
        longitude_nodes=full_era5.sizes['longitude'],
        latitude_spacing=xarray_utils.infer_latitude_spacing(full_era5.latitude),
        longitude_offset=xarray_utils.infer_longitude_offset(full_era5.longitude),
    )
regridder = horizontal_interpolation.ConservativeRegridder(
    era5_grid, model.data_coords.horizontal, skipna=True
)

def run_model(date, date_f, forcings_clim, members):
    print(date)
    year = date.year
    path_gfs_Ini = f"../raw/ncep_ic/processed/gdas_{date_f}.nc"
    gfs_ds = xarray.open_dataset(path_gfs_Ini)

    gfs_date = gfs_ds.sel(time = date)

    
    ## Adam, Please check if I am correct in this
    eval_era5 = xarray_utils.regrid(gfs_date, regridder)
    eval_era5 = xarray_utils.fill_nan_with_nearest(eval_era5)
    forcings_clim_sub = forcings_clim.sel(time=slice(eval_era5.time, eval_era5.time + np.timedelta64(90, 'D')))
    # save outputs 6-hourly
    dt = np.timedelta64(6, 'h')
    # forecast 15 day at a time
    steps = 45 * 24 // 6
    all_forcings = model.forcings_from_xarray(forcings_clim_sub)
    time = eval_era5.time
    for rand in members:
        print(rand)
        init_date = time.values
        times = init_date + (np.arange(1, steps+1) * dt)  # time axis in hours
        
        # initialize model state
        inputs = model.inputs_from_xarray(eval_era5)
        forcing_initial = model.forcings_from_xarray(forcings_clim_sub.isel(time=0))
        rng_key = jax.random.key(rand)
        initial_state = model.encode(inputs, forcing_initial, rng_key)
        
        # make forecast
        state, predictions = model.unroll(
            initial_state,
            all_forcings,
            steps=steps,
            timedelta=dt,
            start_with_input=False,
        )
        predictions_ds = model.data_to_xarray(predictions, times=times)
        
        specific_humidity = predictions_ds[['specific_humidity']].sel(level=[100, 200, 300, 400, 500, 550, 600, 650, 700, 750, 775, 800, 825, 850, 875, 900, 925, 950, 975, 1000])
        geopotential = predictions_ds[['geopotential']].sel(level=[850, 900, 925, 950, 975, 1000])
        wind_850 = predictions_ds[['u_component_of_wind', 'v_component_of_wind']].sel(level=[850])
        precipitation = predictions_ds[['precipitation_cumulative_mean']]
        
        data = xarray.merge([specific_humidity, geopotential, wind_850, precipitation])
        data = data.expand_dims('ensemble', axis=0)
        data['ensemble'] = [rand]
        
        data_rechunked = data.chunk({'time':-1, 'latitude':-1, 'longitude':-1})
        
        data_rechunked.to_zarr(output_path + f'/{date_f}/member_{rand}.zarr')

    print(f"Done with members: {members}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="Year to forecast")
    parser.add_argument("--mpi", type=int, help="MPI rank")
    args = parser.parse_args()
    print("MPI Rank", args.mpi)
    print("Initializing Model for", args.date)
    mpi = args.mpi
    if mpi > 3:
        print("MPI can only be 0, 1, 2, 3")
        return
    os.environ['CUDA_VISIBLE_DEVICES'] = str(mpi)
    devices = jax.local_devices()
    print(devices)
    date_f = args.date
    date = datetime.strptime(date_f, "%Y%m%dT%H")


    forcings_clim = get_forcings_clim(date.year)

    if not os.path.exists(output_path + f'/{date_f}'):
        os.makedirs(output_path + f'/{date_f}', exist_ok=True)
    
    all_members = np.arange(1, N_MEMBERS+1)
    members = np.array_split(all_members, 4)[mpi]
    run_model(date, date_f, forcings_clim, members)

if __name__ == "__main__":
    main()