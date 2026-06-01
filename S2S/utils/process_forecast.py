import argparse
import datetime
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from plot import plot_all

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


def grib_to_netcdf(input_path, output_path):
    """
    Convert GRIB files to NetCDF format using cdo.
    """
    if output_path.exists():
        logging.info(f"Output file {output_path} already exists. Skipping conversion.")
        return
    os.system(f"grib_to_netcdf -T -o {output_path} {input_path}")


def regrid(input_path, output_path, grid_path):
    """
    Regrid NetCDF files to a specified grid using cdo.
    """
    if output_path.exists():
        logging.info(f"Output file {output_path} already exists. Skipping regridding.")
        return
    os.system(f"cdo remapcon,{grid_path} {input_path} {output_path}")


def post_process(regridded_file, final_path, date):
    if final_path.exists():
        logging.info(
            f"Output file {final_path} already exists. Skipping post-processing."
        )
        return
    date = pd.to_datetime(date)
    ds = xr.open_dataset(regridded_file)
    ds_first_val = ds.isel(step=[0])
    ds = ds.diff(dim="step", label="upper")
    ds_combined = xr.concat([ds_first_val, ds], dim="step").sortby("step")
    ds_combined = ds_combined.resample(step="1D").sum()
    ds_combined["step"] = np.arange(len(ds_combined["step"]))
    ds_combined = ds_combined.expand_dims("time")
    ds_combined["time"] = [date]

    ds_combined = ds_combined.rename({"step": "day"})
    ds_combined = ds_combined.transpose("number", "day", "time", "lat", "lon")

    ds_combined = ds_combined.isel(day=slice(0, -1))

    ds_combined.to_netcdf(final_path, mode="w", format="NETCDF4")


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDDHH format",
    )

    args = parser.parse_args()
    date_f = args.date

    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")

    base = Path(__file__).resolve().parent.parent
    raw_data = base / "raw" / "grib"
    processed_data = base / "raw" / "netcdf"
    grid_path = base.parent / "AIFS" / "grids" / "grid_2p0.txt"

    if not processed_data.exists():
        processed_data.mkdir(parents=True, exist_ok=True)

    file = f"ifs_s2s_init_{date_f}"
    final_file = processed_data / f"ifs_s2s_init_{date_f}.nc"

    input_file = raw_data / f"{file}.grib"
    intermediate_file = processed_data / f"{file}_intermediate.nc"
    regridded_file = processed_data / f"{file}_regridded.nc"

    logging.info(f"Converting {input_file} to {intermediate_file}")
    grib_to_netcdf(input_file, intermediate_file)
    logging.info(f"Converted {input_file} to {intermediate_file}")

    logging.info(
        f"Regridding {intermediate_file} to {regridded_file} using grid {grid_path}"
    )
    regrid(intermediate_file, regridded_file, grid_path)
    logging.info(f"Regridded {intermediate_file} to {regridded_file}")

    logging.info(f"Post-processing file: {regridded_file} into {final_file}")
    post_process(regridded_file, final_file, date)
    logging.info(f"Post-processed files saved to {final_file}")
    for file in [intermediate_file, regridded_file]:
        if file.exists():
            logging.info(f"Removing temporary file: {file}")
            file.unlink()
        else:
            logging.warning(f"File {file} does not exist, cannot remove.")
    logging.info("All files processed successfully.")

    logging.info("Plotting results...")
    plot_all(date_f)
    logging.info("Plotting completed.")


if __name__ == "__main__":
    main()
