import xarray as xr
import numpy as np
import os
import argparse
import logging
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)


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


def apply_india_mask(ds, mask_path):
    logging.info("Applying India mask at 0.25° resolution")
    logging.info("Mask convention: 1 = inside India, NaN = outside India")

    # load mask
    mask = xr.open_dataset(mask_path)
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


def process_tp_0p25(ds, mask_path):
    logging.info("Processing TP at 0.25° resolution")

    # Step 1 — clip to India box
    ds = clip_to_india(ds)

    # Step 2 — apply India mask
    ds = apply_india_mask(ds, mask_path)

    # Step 3 — convert to daily
    ds = process_tp(ds)

    return ds


def post_process_india(AIFS, date, model="AIFS"):
    if model != "AIFS":
        logging.info("post_process_india currently only configured for model=AIFS; got %s — skipping", model)
        return
    if len(AIFS.step) > 164:
        logging.info("DS has more than 164 steps "
                     "reformatting to match previous forecast length")
        AIFS = AIFS.isel(step=slice(0, 164))

    output_base_path = "../output/india"
    for subdir in ("sji", "tcw", "tp"):
        os.makedirs(os.path.join(output_base_path, subdir), exist_ok=True)

    grid_file = "../grids/grid_2p0.txt"

    mask_path = "../data/india_mask.nc"

    logging.info("Processing SJI")
    AIFS_SJI = AIFS[["u_850", "v_850"]]
    AIFS_SJI = calculate_sji(AIFS_SJI)
    AIFS_SJI.to_netcdf(f"{output_base_path}/sji/sji_{date}.nc")
    AIFS_SJI.close()

    logging.info("Processing TCW")
    AIFS_TCW = AIFS[["tcw"]]
    AIFS_TCW = set_atts_tcw(AIFS_TCW)
    regrid_input_path = f"{output_base_path}/tcw/{date}_INTERMEDIATE.nc"
    regrid_output_path = f"{output_base_path}/tcw/{date}_INTERMEDIATE_2.nc"
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
    AIFS_TCW.to_netcdf(f"{output_base_path}/tcw/tcw_{date}.nc")
    AIFS_TCW.close()
    os.remove(regrid_output_path)

    logging.info("Processing TP")
    AIFS_tp = AIFS[["tp"]]
    AIFS_tp = set_atts_tp(AIFS_tp)
    regrid_input_path = f"{output_base_path}/tp/{date}_2p0_INTERMEDIATE.nc"
    regrid_output_path = f"{output_base_path}/tp/{date}_2p0_INTERMEDIATE_2.nc"
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
    AIFS_tp.to_netcdf(f"{output_base_path}/tp/tp_2p0_{date}.nc")
    os.remove(regrid_output_path)
    AIFS_tp.close()

    # ── NEW ADDITION — 0.25° India TP ─────────────────────

    logging.info("Processing TP at 0.25° for India domain")

    # take tp fresh from raw AIFS output
    AIFS_tp_0p25 = AIFS[["tp"]]
    AIFS_tp_0p25 = set_atts_tp(AIFS_tp_0p25)

    # clip → mask → daily
    AIFS_tp_0p25 = process_tp_0p25(AIFS_tp_0p25, mask_path)

    # save to tp_0p25 folder
    output_path = f"{output_base_path}/tp/tp_0p25_{date}.nc"
    AIFS_tp_0p25.to_netcdf(output_path)
    AIFS_tp_0p25.close()

    logging.info(f"Saved 0.25° TP to {output_path}")

    # ── CLOSE ──────────────────────────────────────────────

    AIFS.close()

    logging.info(f"Finished post-processing {date}")
    logging.info("Exiting post-processing script")


def subset_ethiopia(ds):
    logging.info("Subsetting to Ethiopia box")
    ds = ds.sel(
        lat=slice(15.0, 3.0),
        lon=slice(33.0, 48.0)
    )
    return ds

def post_process_tp_ethiopia(ds):
    logging.info("Processing daily TP for Ethiopia")
    if "prediction_timedelta" not in ds.coords:
        ds = ds.rename({"step": "prediction_timedelta"})
    ds['valid_time'] = pd.to_datetime(ds["time"].values)[0] + ds["prediction_timedelta"].astype("timedelta64[h]")
    ds = ds.swap_dims({"prediction_timedelta": "valid_time"})
    ds = ds.resample(valid_time="1D").sum()
    ds["valid_time"] = np.arange(len(ds["valid_time"]))
    ds = ds.rename({"valid_time": "day"})
    ds['tp'] = ds['tp'] * 1000 # convert from m to mm
    return ds

def post_process_ethiopia(ds, date, model):
    output_base_path = "../output/ethiopia"
    if not os.path.exists(output_base_path):
        os.makedirs(output_base_path)

    if model == "AIFS":
        output_base_path = f"{output_base_path}/AIFS"
    if model == "AIFS_ENS":
        output_base_path = f"{output_base_path}/AIFS_ENS"
    if not os.path.exists(output_base_path):
        os.makedirs(output_base_path)

    tp_dir = f"{output_base_path}/tp"
    if not os.path.exists(tp_dir):
        os.makedirs(tp_dir)

    ds = ds[["tp"]]
    ds = subset_ethiopia(ds)
    ds = post_process_tp_ethiopia(ds)
    tp_out_path = f"{tp_dir}/tp_0p25_{date}.nc"
    ds.to_netcdf(tp_out_path)
    logging.info(f"Saved Ethiopia Daily TP to {tp_out_path}")

    

REGION_HANDLERS = {
    "india":    post_process_india,
    "ethiopia": post_process_ethiopia,
}


def _load_model_dataset(model, date):
    if model == "AIFS":
        return xr.open_dataset(f"../raw/output/AIFS/init_{date}.nc")
    if model == "AIFS_ENS":
        return xr.open_zarr(f"../raw/output/AIFS_ENS/init_{date}.zarr", chunks={})
    raise ValueError(f"Unknown model: {model!r}")


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="Date for the inference in YYYYMMDDTHH format",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="AIFS",
        help="Model output to process",
    )
    parser.add_argument(
        "--region",
        type=str,
        default=None,
        choices=list(REGION_HANDLERS) + [None],
        help="If given, only process this region. Default: process all regions configured for this model.",
    )
    args = parser.parse_args()
    date = args.date
    model = args.model

    logging.info(f"Processing {date} (model={model}, region={args.region or 'all'})")
    ds = _load_model_dataset(model, date)

    targets = [args.region] if args.region else list(REGION_HANDLERS)
    for region in targets:
        logging.info(f"Running {region} post-processing for model={model}")
        REGION_HANDLERS[region](ds, date, model)

if __name__ == "__main__":
    main()
