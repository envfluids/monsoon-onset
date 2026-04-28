import xarray as xr
import os
import numpy as np
import logging
import argparse
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)


def set_atts_tp(ds):
    ds["lat"].attrs["standard_name"] = "latitude"
    ds["lat"].attrs["units"] = "degrees_north"
    ds["lon"].attrs["standard_name"] = "longitude"
    ds["lon"].attrs["units"] = "degrees_east"
    # ds["time"].attrs["standard_name"] = "time"
    ds["tp"].attrs["standard_name"] = "total_precipitation"
    ds["tp"].attrs["units"] = "mm day-1"
    ds["ensemble"].attrs["standard_name"] = "ensemble_member"
    return ds

def post_process_tp(ds_model_TS, ds_time):
    ds_model_TS['ensemble'] = np.arange(1, len(ds_model_TS.ensemble)+1)
    ds_model_TS = ds_model_TS.expand_dims("time")
    ds_model_TS["time"] = [ds_time]
    ds_model_TS = ds_model_TS.rename({"ensemble": "number"})
    ds_model_TS = ds_model_TS.diff(dim="step", label="upper")
    ds_model_TS["tp"] = ds_model_TS["tp"] * 1000
    ds_model_TS["step"] = ds_model_TS["step"].astype(int)
    ds_model_TS["step"] = ds_model_TS["step"] - 12
    ds_model_TS["day"] = ds_model_TS["step"] // 24
    # Now set 'day' as a coordinate
    ds_model_TS = ds_model_TS.set_coords("day")
    ds_model_TS_daily = ds_model_TS.groupby("day").sum(dim="step")
    ds_model_TS_daily = ds_model_TS_daily.transpose("number", "day", "time", "lat", "lon")
    ds_model_TS_daily["time"].attrs["standard_name"] = "time"
    ds_model_TS_daily["time"].attrs["axis"] = "T"
    return ds_model_TS_daily

def process_data(base, date):
    """
    Process the input dataset, regrid it, and save the output.
    
    Args:
        input_path (str): Path to the input NetCDF file.
        output_path (str): Path to save the processed NetCDF file.
        grid_file (str): Path to the grid file for regridding.
    """
    input_path = base / "raw" / f"{date}.nc"
    output_path = base / "output" / "tp"
    ds = xr.open_dataset(input_path, decode_timedelta=True)
    ds = ds.sel(realization = slice(1, 30))
    ds = ds.rename({"realization":"ensemble", "prediction_timedelta":"step", "total_precipitation":"tp", "latitude": "lat", "longitude": "lon"})
    ds['step'] = np.arange(0, 6 * len(ds.step), 6)
    ds = ds.sel(step = slice(6, 1080))
    ds = ds.isel(time=0)
    ds_time = ds.time.values
    ds = ds.drop_vars(["time", "surface"])
    ds = set_atts_tp(ds)

    regrid_input_path = output_path / f"{date}_INTERMEDIATE.nc"
    regrid_output_path = output_path / f"{date}_INTERMEDIATE_2.nc"
    grid_file = base / "grids" / "grid_2p0.txt"
    ds.to_netcdf(regrid_input_path, unlimited_dims="ensemble")

    command = [
        "cdo",
        "-s",
        f"remapcon,{grid_file}",
        str(regrid_input_path),
        str(regrid_output_path),
    ]
    command = " ".join(command)
    os.system(command)

    output_ds = xr.open_dataset(regrid_output_path)
    processed = post_process_tp(output_ds, ds_time)
    final_output_path = output_path / f"tp_{date}.nc"
    processed.to_netcdf(final_output_path)

    os.remove(regrid_input_path)
    os.remove(regrid_output_path)

def main():
    parser = argparse.ArgumentParser(description="Process and regrid data.")
    parser.add_argument("--date", type=str, required=True, help="Date in format YYYYMMDDTHH")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    date = args.date

    logging.info(f"Processing data for date: {date}")
    process_data(base, date)
    logging.info("Data processing complete.")

if __name__ == "__main__":
    main()
