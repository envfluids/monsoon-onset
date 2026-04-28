import os
from pathlib import Path
import datetime
import argparse
import logging
import xarray as xr
import numpy as np
from plot import plot_all

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

def grib_to_netcdf(input_path, output_path):
    """
    Convert GRIB files to NetCDF format using cdo.
    """
    os.system(f"grib_to_netcdf -T -o {output_path} {input_path}")

def regrid(input_path, output_path, grid_path):
    """
    Regrid NetCDF files to a specified grid using cdo.
    """
    os.system(f"cdo remapcon,{grid_path} {input_path} {output_path}")

def process_cf(input_path, date):
    ds = xr.open_dataset(input_path)
    ds = ds.expand_dims("time")
    ds['time'] = [date]
    ds = ds.isel(step=slice(1,None))
    ds['step'] = np.arange(6, 6 * len(ds.step) + 1, 6)
    ds = ds.expand_dims("number")
    ds['number'] = [0]
    ds = ds.transpose("time", "step", "number", "lat", "lon")
    return ds

def process_pf(input_path, date):
    ds = xr.open_dataset(input_path)
    ds = ds.expand_dims("time")
    ds['time'] = [date]
    ds = ds.isel(step=slice(1,None))
    ds['step'] = np.arange(6, 6 * len(ds.step) + 1, 6)
    return ds

def post_process_tp(ds_model_TS):
    ds_model_TS = ds_model_TS.diff(dim="step", label="upper")
    # ds_model_TS["tp"] = ds_model_TS["tp"] * 1000
    ds_model_TS["step"] = ds_model_TS["step"].astype(int)
    ds_model_TS["step"] = ds_model_TS["step"] - 6
    ds_model_TS["day"] = ds_model_TS["step"] // 24
    # Now set 'day' as a coordinate
    ds_model_TS = ds_model_TS.set_coords("day")
    ds_model_TS_daily = ds_model_TS.groupby("day").sum(dim="step")
    ds_model_TS_daily = ds_model_TS_daily.transpose("number", "day", "time", "lat", "lon")
    return ds_model_TS_daily

def post_process(cf_path, pf_path, final_path, date):
    cf_ds = process_cf(cf_path, date)
    pf_ds = process_pf(pf_path, date)

    output_ds = xr.merge([cf_ds, pf_ds])
    output_ds = post_process_tp(output_ds)
    output_ds.to_netcdf(final_path, mode='w', format='NETCDF4')

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
    
    files = [f"ifs_s2s_cf_init_{date_f}", 
             f"ifs_s2s_pf_init_{date_f}"]
    final = processed_data / f"ifs_s2s_init_{date_f}.nc"
    output_files = []
    regridded_files = []
    for file in files:
        input_file = raw_data / f"{file}.grib"
        output_file = processed_data / f"{file}.nc"
        regridded_file = processed_data / f"{file}_regridded.nc"

        logging.info(f"Converting {input_file} to {output_file}")
        grib_to_netcdf(input_file, output_file)
        output_files.append(output_file)
        logging.info(f"Converted {input_file} to {output_file}")

        logging.info(f"Regridding {output_file} to {regridded_file} using grid {grid_path}")
        regrid(output_file, regridded_file, grid_path)
        regridded_files.append(regridded_file)
        logging.info(f"Regridded {output_file} to {regridded_file}")


    logging.info(f"Post-processing files: {[str(f) for f in regridded_files]} into {final}")
    post_process(regridded_files[0], regridded_files[1], final, date)
    logging.info(f"Post-processed files saved to {final}")
    for file in output_files + regridded_files:
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
