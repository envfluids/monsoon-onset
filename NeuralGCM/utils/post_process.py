import xarray as xr
import numpy as np
import os
import argparse

# from cdo import *
import glob
from concurrent.futures import (
    ProcessPoolExecutor,
    as_completed,
)  # Import necessary components
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

grid_file = "../grids/grid_2p0.txt"

# def calculate_sji(ds):
#     wind_850 = ds.sel(lat=slice(-5., 21.),lon=slice(50.,71.)) # select the SJI region
#     wind_speed = (wind_850.v_component_of_wind**2 + wind_850.u_component_of_wind **2) * 0.5 # calculate the wind speed
#     mean_wind_speed = wind_speed.mean(dim=['lat', 'lon']) # calculate the mean wind speed over the region
#     mean_wind_speed = np.sqrt(mean_wind_speed * 2) # convert the wind speed to m/s, the SJI
#     mean_wind_speed.name = "sji"
#     mean_wind_speed = mean_wind_speed.to_dataset()
#     mean_wind_speed['step'] = mean_wind_speed['step'].astype(int)
#     mean_wind_speed['step'] = mean_wind_speed['step'] - 12
#     mean_wind_speed['day'] = mean_wind_speed['step'] // 24
#     mean_wind_speed = mean_wind_speed.set_coords('day')
#     mean_wind_speed = mean_wind_speed.groupby('day').mean(dim='step')
#     return mean_wind_speed


def set_atts_tcw(ds):
    ds["lat"].attrs["standard_name"] = "latitude"
    ds["lat"].attrs["units"] = "degrees_north"
    ds["lon"].attrs["standard_name"] = "longitude"
    ds["lon"].attrs["units"] = "degrees_east"
    ds["time"].attrs["standard_name"] = "time"
    ds["tcw"].attrs["standard_name"] = "total_column_water_vapor"
    ds["tcw"].attrs["units"] = "kg m-2"
    return ds


def set_atts_tp(ds):
    ds["lat"].attrs["standard_name"] = "latitude"
    ds["lat"].attrs["units"] = "degrees_north"
    ds["lon"].attrs["standard_name"] = "longitude"
    ds["lon"].attrs["units"] = "degrees_east"
    ds["time"].attrs["standard_name"] = "time"
    ds["tp"].attrs["standard_name"] = "total_precipitation"
    ds["tp"].attrs["units"] = "mm day-1"
    return ds


def post_process_tp(ds_model_TS):
    ds_model_TS = ds_model_TS.diff(dim="step", label="upper")
    ds_model_TS["tp"] = ds_model_TS["tp"] * 1000
    ds_model_TS["step"] = ds_model_TS["step"].astype(int)
    ds_model_TS["step"] = ds_model_TS["step"] - 12
    ds_model_TS["day"] = ds_model_TS["step"] // 24
    # Now set 'day' as a coordinate
    ds_model_TS = ds_model_TS.set_coords("day")
    ds_model_TS_daily = ds_model_TS.groupby("day").sum(dim="step")
    ds_model_TS_daily = ds_model_TS_daily.transpose("day", "time", "lat", "lon")
    return ds_model_TS_daily


def process_tcw(ds):
    delta_pressure = ds["level"].diff("level") * 100  # Convert from hPa to Pa
    tcwv = (ds["specific_humidity"] * delta_pressure).sum(dim="level") / 9.81
    tcwv = tcwv.rename("tcw")
    tcwv = tcwv.to_dataset()
    return tcwv


def post_process_tcw(tcwv):
    tcwv["step"] = tcwv["step"].astype(int)
    tcwv["step"] = tcwv["step"] - 6
    tcwv["day"] = tcwv["step"] // 24
    tcwv = tcwv.set_coords("day")
    tcwv = tcwv.groupby("day").mean(dim="step")
    return tcwv


def preprocess(ds):
    time_first_value = ds["time"].values[0] - np.timedelta64(6, "h")
    ds = ds.rename({"time": "step"})
    ds["step"] = np.arange(6, 6 * len(ds.step) + 1, 6)
    ds = ds.expand_dims("time")  # Ensure step is a dimension
    ds["time"] = [time_first_value]
    return ds


def process_member(member, date):
    # cdo = Cdo()
    print("Processing member:", member)
    ds = (
        xr.open_zarr(f"../raw/output/{date}/member_{member}.zarr")
        .rename(
            {
                "precipitation_cumulative_mean": "tp",
                "latitude": "lat",
                "longitude": "lon",
            }
        )
        .isel(ensemble=0, surface=0)
    )
    ds = preprocess(ds)
    ds_tcw = process_tcw(ds[["specific_humidity"]])
    ds_tcw = set_atts_tcw(ds_tcw)
    regrid_input_path = f"../output/tcw/{member}_{date}_INTERMEDIATE.nc"
    regrid_output_path = f"../output/tcw/{member}_{date}_INTERMEDIATE_2.nc"
    ds_tcw = ds_tcw.transpose("time", "lat", "lon", ...)
    ds_tcw.to_netcdf(regrid_input_path)
    # cdo.remapcon(grid_file, input=regrid_input_path, output=regrid_output_path)
    command = [
        "cdo",
        "-s",
        f"remapcon,{grid_file}",
        regrid_input_path,
        regrid_output_path,
    ]
    command = " ".join(command)
    os.system(command)
    os.remove(regrid_input_path)
    ds_tcw = xr.open_dataset(regrid_output_path)
    ds_tcw = post_process_tcw(ds_tcw)
    ds_tcw = ds_tcw.expand_dims("number")
    ds_tcw["number"] = [member]
    member_output_path = f"../output/tcw/{member}_{date}_INTERMEDIATE_3.nc"
    ds_tcw.to_netcdf(member_output_path)
    ds_tcw.close()
    os.remove(regrid_output_path)

    ds_tp = set_atts_tp(ds[["tp"]])
    regrid_input_path = f"../output/tp/{member}_{date}_INTERMEDIATE.nc"
    regrid_output_path = f"../output/tp/{member}_{date}_INTERMEDIATE_2.nc"
    ds_tp = ds_tp.transpose("time", "lat", "lon", ...)
    ds_tp.to_netcdf(regrid_input_path)
    # cdo.remapcon(grid_file, input=regrid_input_path, output=regrid_output_path)
    command = [
        "cdo",
        "-s",
        f"remapcon,{grid_file}",
        regrid_input_path,
        regrid_output_path,
    ]
    command = " ".join(command)
    os.system(command)
    os.remove(regrid_input_path)
    ds_tp = xr.open_dataset(regrid_output_path)
    ds_tp = post_process_tp(ds_tp)
    ds_tp.expand_dims("number")
    ds_tp["number"] = [member]
    member_output_path = f"../output/tp/{member}_{date}_INTERMEDIATE_3.nc"
    ds_tp.to_netcdf(member_output_path)
    ds_tp.close()
    os.remove(regrid_output_path)
    return member


def main():
    # cdo = Cdo()
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDDTHH format",
    )
    args = parser.parse_args()
    date = args.date

    n_members = len(glob.glob(f"../raw/output/{date}/member_*.zarr"))
    futures = []
    with ProcessPoolExecutor(max_workers=n_members) as executor:
        print(
            f"Submitting tasks for {n_members} members using up to {n_members} workers..."
        )
        # Submit all tasks for the current year
        for member in range(1, n_members + 1):
            # executor.submit schedules the function to run and returns a Future object
            future = executor.submit(process_member, member, date)
            futures.append(future)

        # --- Collect results ---
        processed_files = []
        print(f"Waiting for {len(futures)} member tasks to complete for {date}...")
        # as_completed yields futures as they finish (in any order)
        # This is useful for getting results sooner or for progress tracking
        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = (
                    future.result()
                )  # Get the return value from the function (file path or None)
                print(
                    f"  Completed task {i}/{n_members} for {date}. Success: {result is not None}"
                )
                if result:
                    processed_files.append(result)
            except Exception as e:
                # Catch exceptions raised *during* the execution of the task in the worker process
                print(
                    f"ERROR retrieving result for a task in {date}: {type(e).__name__} - {e}"
                )
                # Log the specific member if possible (though future doesn't easily expose args)
                # traceback.print_exc() # Optionally print traceback

    # for member in range(1, n_members+1):
    #     process_member(member, date)


if __name__ == "__main__":
    main()
