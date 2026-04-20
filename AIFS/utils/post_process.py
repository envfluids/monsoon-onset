import xarray as xr
import numpy as np
import os
import argparse
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

grid_file = "../grids/grid_2p0.txt"
mask_file = "../data/india_mask.nc"


def calculate_sji(ds):
    wind_850 = ds.sel(
        lat=slice(20.0, -5.0), lon=slice(50.0, 70.0)
    )
    wind_speed = (
        wind_850.u_850**2 + wind_850.v_850**2
    ) * 0.5
    mean_wind_speed = wind_speed.mean(
        dim=["lat", "lon"]
    )
    mean_wind_speed = np.sqrt(
        mean_wind_speed * 2
    )
    mean_wind_speed.name = "sji"
    mean_wind_speed = mean_wind_speed.to_dataset()
    mean_wind_speed["step"] = mean_wind_speed["step"].astype(int)
    mean_wind_speed["step"] = mean_wind_speed["step"] - 6
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
    ds_model_TS["step"] = ds_model_TS["step"] - 6
    ds_model_TS["day"] = ds_model_TS["step"] // 24
    ds_model_TS = ds_model_TS.set_coords("day")
    ds_model_TS_daily = ds_model_TS.groupby("day").sum(dim="step")
    ds_model_TS_daily = ds_model_TS_daily.transpose("day", "time", "lat", "lon")
    return ds_model_TS_daily


def process_tcw(ds):
    ds["step"] = ds["step"].astype(int)
    ds["step"] = ds["step"] - 6
    ds["day"] = ds["step"] // 24
    ds = ds.set_coords("day")
    ds = ds.groupby("day").mean(dim="step")
    return ds


def clip_to_india(ds):
    logging.info("Clipping to India box at native 0.25° resolution")
    ds = ds.sel(
        lat=slice(38.5, 6.5),
        lon=slice(66.5, 100.0)
    )
    logging.info(f"Clipped shape: {ds['tp'].shape}")
    logging.info(f"Lat range: {float(ds.lat.min()):.2f} to {float(ds.lat.max()):.2f}")
    logging.info(f"Lon range: {float(ds.lon.min()):.2f} to {float(ds.lon.max()):.2f}")
    return ds


def apply_india_mask(ds):
    logging.info("Applying India mask at 0.25° resolution")
    logging.info("Mask convention: 1 = inside India, NaN = outside India")

    # load mask
    mask = xr.open_dataset(mask_file)
    mask_data = mask["lsm"]

    # convert mask coords to float
    # to match clipped ds coords
    mask_data["lat"] = mask_data["lat"].astype(float)
    mask_data["lon"] = mask_data["lon"].astype(float)

    logging.info(f"Mask shape: {mask_data.shape}")
    logging.info(f"DS shape:   {ds['tp'].shape}")

    # apply mask:
    # lsm = 1   → inside India  → keep tp value
    # lsm = NaN → outside India → set tp to NaN
    ds["tp"] = ds["tp"].where(mask_data == 1.0)

    mask.close()
    logging.info("India mask applied successfully")
    return ds


def process_tp_0p25(ds):
    logging.info("Processing TP at 0.25° resolution")

    # Step 1 — clip to India box
    ds = clip_to_india(ds)

    # Step 2 — apply India mask
    ds = apply_india_mask(ds)

    # Step 3 — convert to daily
    ds = process_tp(ds)

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

    logging.info(f"Processing {date}")
    logging.info("Loading AIFS data")
    AIFS = xr.open_dataset(f"../raw/output/init_{date}.nc")

    # ── ORIGINAL PROCESSING — unchanged ───────────────────

    logging.info("Processing SJI")
    AIFS_SJI = AIFS[["u_850", "v_850"]]
    AIFS_SJI = calculate_sji(AIFS_SJI)
    AIFS_SJI.to_netcdf(f"../output/sji/sji_{date}.nc")
    AIFS_SJI.close()

    logging.info("Processing TCW")
    AIFS_TCW = AIFS[["tcw"]]
    AIFS_TCW = set_atts_tcw(AIFS_TCW)
    regrid_input_path = f"../output/{date}_INTERMEDIATE.nc"
    regrid_output_path = f"../output/tcw/{date}_INTERMEDIATE_2.nc"
    AIFS_TCW.to_netcdf(regrid_input_path)
    logging.info("Regridding TCW")
    command = [
        "cdo", "-s",
        f"remapcon,{grid_file}",
        regrid_input_path,
        regrid_output_path,
    ]
    command = " ".join(command)
    os.system(command)
    os.remove(regrid_input_path)
    AIFS_TCW = xr.open_dataset(regrid_output_path)
    AIFS_TCW = process_tcw(AIFS_TCW)
    AIFS_TCW.to_netcdf(f"../output/tcw/tcw_{date}.nc")
    AIFS_TCW.close()
    os.remove(regrid_output_path)

    logging.info("Processing TP")
    AIFS_tp = AIFS[["tp"]]
    AIFS_tp = set_atts_tp(AIFS_tp)
    regrid_input_path = f"../output/tp/{date}_INTERMEDIATE.nc"
    regrid_output_path = f"../output/tp/{date}_INTERMEDIATE_2.nc"
    AIFS_tp.to_netcdf(regrid_input_path)
    logging.info("Regridding TP")
    command = [
        "cdo", "-s",
        f"remapcon,{grid_file}",
        regrid_input_path,
        regrid_output_path,
    ]
    command = " ".join(command)
    os.system(command)
    os.remove(regrid_input_path)
    AIFS_tp = xr.open_dataset(regrid_output_path)
    AIFS_tp = process_tp(AIFS_tp)
    AIFS_tp.to_netcdf(f"../output/tp/tp_{date}.nc")
    os.remove(regrid_output_path)
    AIFS_tp.close()

    # ── NEW ADDITION — 0.25° India TP ─────────────────────

    logging.info("Processing TP at 0.25° for India domain")

    # take tp fresh from raw AIFS output
    AIFS_tp_0p25 = AIFS[["tp"]]
    AIFS_tp_0p25 = set_atts_tp(AIFS_tp_0p25)

    # clip → mask → daily
    AIFS_tp_0p25 = process_tp_0p25(AIFS_tp_0p25)

    # save to tp_0p25 folder
    output_path = f"../output/tp_0p25/tp_{date}.nc"
    AIFS_tp_0p25.to_netcdf(output_path)
    AIFS_tp_0p25.close()

    logging.info(f"Saved 0.25° TP to {output_path}")

    # ── CLOSE ──────────────────────────────────────────────

    AIFS.close()

    logging.info(f"Finished post-processing {date}")
    logging.info("Exiting post-processing script")


if __name__ == "__main__":
    main()
