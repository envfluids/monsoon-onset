import xarray as xr
import glob
import argparse
import numpy as np
import os


def calculate_sji(ds):
    wind_850 = ds.sel(
        lat=slice(-5.0, 21.0), lon=slice(50.0, 71.0)
    )  # select the SJI region
    wind_speed = (
        wind_850.v_component_of_wind**2 + wind_850.u_component_of_wind**2
    ) * 0.5  # calculate the wind speed
    mean_wind_speed = wind_speed.mean(
        dim=["lat", "lon"]
    )  # calculate the mean wind speed over the region
    mean_wind_speed = np.sqrt(
        mean_wind_speed * 2
    )  # convert the wind speed to m/s, the SJI
    mean_wind_speed.name = "sji"
    mean_wind_speed = mean_wind_speed.to_dataset()
    mean_wind_speed["step"] = mean_wind_speed["step"].astype(int)
    mean_wind_speed["step"] = mean_wind_speed["step"] - 6
    mean_wind_speed["day"] = mean_wind_speed["step"] // 24
    mean_wind_speed = mean_wind_speed.set_coords("day")
    mean_wind_speed = mean_wind_speed.groupby("day").mean(dim="step")
    return mean_wind_speed


def preprocess(ds):
    time_first_value = ds["time"].values[0] - np.timedelta64(6, "h")
    ds = ds.rename({"time": "step"})
    ds["step"] = np.arange(6, 6 * len(ds.step) + 1, 6)
    ds = ds.expand_dims("time")  # Ensure step is a dimension
    ds["time"] = [time_first_value]
    return ds


def main():
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
    ngcm_out = xr.open_mfdataset(
        f"../raw/output/{date}/*.zarr", engine="zarr", preprocess=preprocess
    )
    ngcm_out = ngcm_out.rename({"latitude": "lat", "longitude": "lon"})
    ngcm_out = (
        ngcm_out[["v_component_of_wind", "u_component_of_wind"]]
        .sel(level=850)
        .drop_vars("level")
    )
    ngcm_out = calculate_sji(ngcm_out)
    ngcm_out.to_netcdf(f"../output/sji/{date}.nc")
    ngcm_out.close()

    tcw_files = glob.glob(f"../output/tcw/*_{date}_INTERMEDIATE_3.nc")
    tcw = xr.open_mfdataset(tcw_files)
    tcw.to_netcdf(f"../output/tcw/{date}.nc")
    tcw.close()

    tp_files = glob.glob(f"../output/tp/*_{date}_INTERMEDIATE_3.nc")
    tp = xr.open_mfdataset(tp_files)
    tp.to_netcdf(f"../output/tp/{date}.nc")
    tp.close()

    for file in tcw_files:
        os.remove(file)
    for file in tp_files:
        os.remove(file)


if __name__ == "__main__":
    main()
