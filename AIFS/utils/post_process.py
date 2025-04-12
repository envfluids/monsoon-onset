import xarray as xr
import numpy as np
import os
import argparse
# from cdo import *


grid_file = "../grids/grid_2p0.txt"


def calculate_sji(ds):
    wind_850 = ds.sel(
        lat=slice(20.0, -5.0), lon=slice(50.0, 70.0)
    )  # select the SJI region
    wind_speed = (
        wind_850.u_850**2 + wind_850.v_850**2
    ) * 0.5  # calculate the wind speed
    mean_wind_speed = wind_speed.mean(
        dim=["lat", "lon"]
    )  # calculate the mean wind speed over the region
    mean_wind_speed = np.sqrt(
        mean_wind_speed * 2
    )  # convert the wind speed to m/s, the SJI
    # display(mean_wind_speed)
    mean_wind_speed.name = "sji"
    mean_wind_speed = mean_wind_speed.to_dataset()
    mean_wind_speed["step"] = mean_wind_speed["step"].astype(int)
    mean_wind_speed["step"] = mean_wind_speed["step"] - 12
    mean_wind_speed["day"] = mean_wind_speed["step"] // 24
    mean_wind_speed = mean_wind_speed.set_coords("day")
    mean_wind_speed = mean_wind_speed.groupby("day").mean(dim="step")
    return mean_wind_speed


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


def process_tp(ds_model_TS):
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
    ds["step"] = ds["step"].astype(int)
    ds["step"] = ds["step"] - 12
    ds["day"] = ds["step"] // 24
    ds = ds.set_coords("day")
    ds = ds.groupby("day").mean(dim="step")
    return ds


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

    print(f"Processing {date}")
    print("Loading AIFS data")
    AIFS = xr.open_dataset(f"../raw/output/init_{date}.nc")

    print("Processing SJI")
    AIFS_SJI = AIFS[["u_850", "v_850"]]
    AIFS_SJI = calculate_sji(AIFS_SJI)
    AIFS_SJI.to_netcdf(f"../output/sji/{date}.nc")
    AIFS_SJI.close()

    print("Processing TCW")
    AIFS_TCW = AIFS[["tcw"]]
    AIFS_TCW = set_atts_tcw(AIFS_TCW)
    regrid_input_path = f"../output/{date}_INTERMEDIATE.nc"
    regrid_output_path = f"../output/tcw/{date}_INTERMEDIATE_2.nc"
    AIFS_TCW.to_netcdf(regrid_input_path)
    print("Regridding TCW")
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
    AIFS_TCW = xr.open_dataset(regrid_output_path)
    AIFS_TCW = process_tcw(AIFS_TCW)
    AIFS_TCW.to_netcdf(f"../output/tcw/{date}.nc")
    AIFS_TCW.close()
    os.remove(regrid_output_path)

    print("Processing TP")
    AIFS_tp = AIFS[["tp"]]
    AIFS_tp = set_atts_tp(AIFS_tp)
    regrid_input_path = f"../output/tp/{date}_INTERMEDIATE.nc"
    regrid_output_path = f"../output/tp/{date}_INTERMEDIATE_2.nc"
    AIFS_tp.to_netcdf(regrid_input_path)
    print("Regridding TP")
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
    AIFS_tp = xr.open_dataset(regrid_output_path)
    AIFS_tp = process_tp(AIFS_tp)
    AIFS_tp.to_netcdf(f"../output/tp/{date}.nc")
    os.remove(regrid_output_path)
    AIFS_tp.close()
    AIFS.close()

    print(f"Finished processing {date}")


if __name__ == "__main__":
    main()
