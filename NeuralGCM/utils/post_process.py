import argparse
import logging
import os
from concurrent.futures import (
    ProcessPoolExecutor,
    as_completed,
)
from pathlib import Path

import numpy as np
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

grid_file = "../grids/grid_2p0.txt"
# ── NEW ───────────────────────────────────────────────────────────────────────
grid_0p25_india_file = "../grids/grid_0p25_india.txt"
mask_path = "../data/india_mask.nc"
# ─────────────────────────────────────────────────────────────────────────────

RAW_OUTPUT_BASE = Path(os.environ.get("NEURALGCM_RAW_OUTPUT_DIR", "../output/raw"))


def raw_forecast_path(date):
    return RAW_OUTPUT_BASE / f"{date}.zarr"


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
    ds = ds.expand_dims("time")
    ds["time"] = [time_first_value]
    return ds


# ── NEW ───────────────────────────────────────────────────────────────────────
def process_tp_0p25_india(ds, date, member, output_base):
    """
    Regrid NeuralGCM TP from native Gaussian ~2.8° to 0.25° over India,
    apply diff (cumulative → incremental), compute daily sums,
    and apply the India land-sea mask.
    Saves a per-member intermediate file for later merging.
    """
    member_output_path = f"{output_base}/tp/{member}_{date}_0p25_INTERMEDIATE_3.nc"
    if os.path.exists(member_output_path):
        logging.info(
            f"{member_output_path} already exists. "
            f"Skipping 0.25° TP processing for member {member}."
        )
        return

    # prepare dataset for CDO
    ds_tp = ds[["tp"]].copy()
    ds_tp = ds_tp.drop_vars(["surface", "ensemble"], errors="ignore")

    # CF attributes so CDO recognises lat/lon as spatial coordinates
    ds_tp["lat"].attrs = {"standard_name": "latitude",  "units": "degrees_north"}
    ds_tp["lon"].attrs = {"standard_name": "longitude", "units": "degrees_east"}

    ds_tp = set_atts_tp(ds_tp)
    ds_tp = ds_tp.transpose("time", "step", "lat", "lon")
    ds_tp.attrs = {}

    # save intermediate → regrid → remove intermediate
    regrid_input_path  = f"{output_base}/tp/{member}_{date}_0p25_INTERMEDIATE.nc"
    regrid_output_path = f"{output_base}/tp/{member}_{date}_0p25_INTERMEDIATE_2.nc"
    ds_tp.to_netcdf(regrid_input_path)

    command = " ".join([
        "cdo", "-s",
        f"remapcon,{grid_0p25_india_file}",
        regrid_input_path,
        regrid_output_path,
    ])
    logging.info(f"Regridding TP to 0.25° for member {member}")
    os.system(command)
    os.remove(regrid_input_path)

    # load regridded output
    ds_tp = xr.open_dataset(regrid_output_path)

    # diff: cumulative → incremental precipitation
    ds_tp = ds_tp.diff(dim="step", label="upper")

    # convert m → mm and compute daily sums
    ds_tp["tp"] = ds_tp["tp"] * 1000
    ds_tp["step"] = ds_tp["step"].astype(int) - 12
    ds_tp["day"] = ds_tp["step"] // 24
    ds_tp = ds_tp.set_coords("day")
    ds_tp = ds_tp.groupby("day").sum(dim="step")
    ds_tp = ds_tp.transpose("day", "time", "lat", "lon")

    # apply India land-sea mask
    mask = xr.open_dataset(mask_path)
    mask_data = mask["lsm"]
    mask_data["lat"] = mask_data["lat"].astype(float)
    mask_data["lon"] = mask_data["lon"].astype(float)
    ds_tp["tp"] = ds_tp["tp"].where(mask_data == 1.0)
    mask.close()

    # add member dimension and save
    ds_tp = ds_tp.expand_dims("number")
    ds_tp["number"] = [member]
    ds_tp.to_netcdf(member_output_path)
    ds_tp.close()
    os.remove(regrid_output_path)

    logging.info(f"Saved 0.25° India TP for member {member} to {member_output_path}")
# ─────────────────────────────────────────────────────────────────────────────


def process_member(member, date):
    logging.info(f"Processing member: {member}")
    region = "india"
    output_base = f"../output/{region}"
    os.makedirs(f"{output_base}", exist_ok=True)
    os.makedirs(f"{output_base}/tp", exist_ok=True)
    os.makedirs(f"{output_base}/tcw", exist_ok=True)
    ds = (
        xr.open_zarr(raw_forecast_path(date))
        .rename(
            {
                "precipitation_cumulative_mean": "tp",
                "latitude": "lat",
                "longitude": "lon",
            }
        )
        .sel(ensemble=member)
        .isel(surface=0)
    )
    ds = preprocess(ds)

    tcw_final_file = f"{output_base}/tcw/tcw_{date}.nc"
    if os.path.exists(tcw_final_file):
        logging.info(
            f"{tcw_final_file} already exists. Skipping TCW processing for member {member}."
        )
    else:
        ds_tcw = process_tcw(ds[["specific_humidity"]])
        ds_tcw = set_atts_tcw(ds_tcw)
        regrid_input_path = f"{output_base}/tcw/{member}_{date}_INTERMEDIATE.nc"
        regrid_output_path = f"{output_base}/tcw/{member}_{date}_INTERMEDIATE_2.nc"
        ds_tcw = ds_tcw.transpose("time", "lat", "lon", ...)
        ds_tcw.to_netcdf(regrid_input_path)
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
        member_output_path = f"{output_base}/tcw/{member}_{date}_INTERMEDIATE_3.nc"
        ds_tcw.to_netcdf(member_output_path)
        ds_tcw.close()
        os.remove(regrid_output_path)

    tp_final_file = f"{output_base}/tp/tp_2p0_{date}.nc"
    if os.path.exists(tp_final_file):
        logging.info(
            f"{tp_final_file} already exists. Skipping TP processing for member {member}."
        )
    else:
        ds_tp = set_atts_tp(ds[["tp"]])
        regrid_input_path = f"{output_base}/tp/{member}_{date}_INTERMEDIATE.nc"
        regrid_output_path = f"{output_base}/tp/{member}_{date}_INTERMEDIATE_2.nc"
        ds_tp = ds_tp.transpose("time", "lat", "lon", ...)
        ds_tp.to_netcdf(regrid_input_path)
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
        ds_tp = ds_tp.expand_dims("number")
        ds_tp["number"] = [member]
        member_output_path = f"{output_base}/tp/{member}_{date}_INTERMEDIATE_3.nc"
        ds_tp.to_netcdf(member_output_path)
        ds_tp.close()
        os.remove(regrid_output_path)

    # ── NEW ───────────────────────────────────────────────────────────────────
    process_tp_0p25_india(ds, date, member, output_base)
    # ─────────────────────────────────────────────────────────────────────────

    return member


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

    raw_ds = xr.open_zarr(raw_forecast_path(date))
    members = [int(member) for member in raw_ds["ensemble"].values]
    raw_ds.close()
    n_members = len(members)
    futures = []
    with ProcessPoolExecutor(max_workers=n_members) as executor:
        logging.info(
            f"Submitting tasks for {n_members} members using up to {n_members} workers..."
        )
        for member in members:
            future = executor.submit(process_member, member, date)
            futures.append(future)

        processed_files = []
        logging.info(
            f"Waiting for {len(futures)} member tasks to complete for {date}..."
        )
        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                logging.info(
                    f"  Completed task {i}/{n_members} for {date}. Success: {result is not None}"
                )
                if result:
                    processed_files.append(result)
            except Exception as e:
                logging.info(
                    f"ERROR retrieving result for a task in {date}: {type(e).__name__} - {e}"
                )


if __name__ == "__main__":
    main()