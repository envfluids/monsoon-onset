import argparse
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE = Path(__file__).parent.parent
FCST_DIR = Path(os.getenv("GENCAST_OUTPUT_DIR", BASE / "raw" / "output"))


def subset_ethiopia(ds):
    logging.info("Subsetting to Ethiopia box")
    ds = ds.sel(lat=slice(15.0, 3.0), lon=slice(33.0, 48.0))
    return ds


def post_process_tp_ethiopia(ds):
    logging.info("Processing daily TP for Ethiopia")
    if "prediction_timedelta" not in ds.coords:
        ds = ds.rename({"step": "prediction_timedelta"})
    ds["valid_time"] = pd.to_datetime(ds["time"].values)[0] + ds[
        "prediction_timedelta"
    ].astype("timedelta64[h]")
    ds = ds.swap_dims({"prediction_timedelta": "valid_time"})
    ds = ds.resample(valid_time="1D").sum()
    ds["valid_time"] = np.arange(len(ds["valid_time"]))
    ds = ds.rename({"valid_time": "day"})
    ds["tp"] = ds["tp"] * 1000  # convert from m to mm
    return ds


def format_ds(ds, date_f):
    ds = ds.rename({"time": "prediction_timedelta"})
    if "sample" in ds.coords:
        ds = ds.rename({"sample": "number"})
    ds = ds.expand_dims({"time": [pd.to_datetime(date_f)]})
    if "batch" in ds.coords:
        ds = ds.isel(batch=0)
    ds = ds.sortby("lon").sortby("lat", ascending=False)
    return ds


def post_process_ethiopia(ds, date):
    output_base_path = BASE / "output" / "ethiopia"
    output_base_path.mkdir(parents=True, exist_ok=True)

    tp_dir = output_base_path / "tp"
    tp_dir.mkdir(parents=True, exist_ok=True)

    ds = ds[["tp"]]
    ds = format_ds(ds, date)
    ds = subset_ethiopia(ds)
    ds = post_process_tp_ethiopia(ds)
    tp_out_path = tp_dir / f"tp_0p25_{date}.nc"

    if tp_out_path.exists():
        logging.warning(f"{tp_out_path} already exists and will be overwritten.")
        tp_out_path.unlink()

    ds.to_netcdf(tp_out_path)
    logging.info(f"Saved Ethiopia Daily TP to {tp_out_path}")


REGION_HANDLERS = {
    "ethiopia": post_process_ethiopia,
}


def _load_model_dataset(date):
    fcst_path = FCST_DIR / f"init_{date}.zarr"
    if not fcst_path.exists():
        raise FileNotFoundError(f"Forecast dataset not found at {fcst_path}")
    logging.info(f"Loading forecast dataset from {fcst_path}")
    return xr.open_zarr(fcst_path)


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
        "--region",
        type=str,
        default=None,
        choices=list(REGION_HANDLERS) + [None],
        help="If given, only process this region. Default: process all regions configured for this model.",
    )
    args = parser.parse_args()
    date = args.date
    region = args.region

    logging.info(f"Processing {date} (region={region or 'all'})")
    ds = _load_model_dataset(date)

    targets = [region] if region else list(REGION_HANDLERS)
    for region in targets:
        logging.info(f"Running {region} post-processing")
        REGION_HANDLERS[region](ds, date)


if __name__ == "__main__":
    main()
