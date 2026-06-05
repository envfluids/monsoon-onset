import argparse
import logging
import os
from pathlib import Path

import numpy as np
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE = Path(__file__).parent.parent
RAW_OUTPUT_BASE = Path(os.environ.get("NEURALGCM_RAW_OUTPUT_DIR", BASE / "output" / "raw"))


def calculate_sji(ds):
    wind_850 = ds.sel(
        lat=slice(-5.0, 21.0), lon=slice(50.0, 71.0)
    )
    wind_speed = (
        wind_850.v_component_of_wind**2 + wind_850.u_component_of_wind**2
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


def preprocess(ds):
    time_first_value = ds["time"].values[0] - np.timedelta64(6, "h")
    ds = ds.rename({"time": "step"})
    ds["step"] = np.arange(6, 6 * len(ds.step) + 1, 6)
    ds = ds.expand_dims("time")
    ds["time"] = [time_first_value]
    return ds


def post_process_ethiopia(ds, date):
    region = "ethiopia"
    output_base = BASE / "output" / region
    out_dir_tp = output_base / "tp"

    output_base.mkdir(parents=True, exist_ok=True)
    out_dir_tp.mkdir(parents=True, exist_ok=True)

    tp_out_path = out_dir_tp / f"tp_2p8_{date}.nc"

    if tp_out_path.exists():
        logging.info(f"{tp_out_path} already exists. Skipping tp post-processing for Ethiopia.")
    else:
        ds = (
            ds[["precipitation_cumulative_mean"]]
            .isel(surface=0)
            .drop_vars("surface")
            .rename(
                {
                    "longitude": "lon",
                    "latitude": "lat",
                    "precipitation_cumulative_mean": "tp",
                    "ensemble": "number",
                }
            )
            .sel(lat=slice(1, 16), lon=slice(30, 51), number=slice(1, 25))
        )
        first_step = xr.zeros_like(ds.isel(step=0))
        ds = xr.concat([first_step, ds], dim='step')
        if "step" in ds.coords:
            ds = ds.rename({"step": "prediction_timedelta"})
        ds["valid_time"] = ds["time"].values[0] + ds["prediction_timedelta"].astype(
            "timedelta64[h]"
        )
        ds = ds.swap_dims({"prediction_timedelta": "valid_time"})
        ds = ds.diff("valid_time")
        ds = ds.resample(valid_time="1D").sum()
        ds["valid_time"] = np.arange(len(ds["valid_time"]))
        ds = ds.rename({"valid_time": "day"})
        ds["tp"] = ds["tp"] * 1000
        ds = ds.transpose("time", "number", "day", "lon", "lat")

        ds.to_netcdf(out_dir_tp / f"tp_2p8_{date}.nc")
        logging.info(f"Saved tp for Ethiopia to {out_dir_tp / f'tp_2p8_{date}.nc'}")

    return


def post_process_india(ds, date):

    region = "india"
    output_base = BASE / "output" / region
    output_base.mkdir(parents=True, exist_ok=True)

    intermediate_pattern = f"*_{date}_INTERMEDIATE_3.nc"

    SJI_out_path = output_base / "sji" / f"sji_{date}.nc"
    if SJI_out_path.exists():
        logging.info(f"{SJI_out_path} already exists. Skipping SJI calculation.")
    else:
        logging.info(f"Merging data for date: {date}")
        ds = ds.rename({"latitude": "lat", "longitude": "lon"})
        ds = (
            ds[["v_component_of_wind", "u_component_of_wind"]]
            .sel(level=850)
            .drop_vars("level")
        )
        logging.info("Calculating & Merging SJI")
        ds = calculate_sji(ds)
        ds.to_netcdf(SJI_out_path)
        ds.close()

    tcw_in_path = output_base / "tcw"
    tcw_out_path = tcw_in_path / f"tcw_{date}.nc"
    if tcw_out_path.exists():
        logging.info(f"{tcw_out_path} already exists. Skipping tcw merging.")
    else:
        logging.info("Merging tcwv")
        tcw_files = list(tcw_in_path.glob(intermediate_pattern))
        tcw = xr.open_mfdataset(tcw_files, engine="h5netcdf")
        tcw.to_netcdf(tcw_out_path)
        tcw.close()
        logging.info("Removing intermediate files")
        for file in tcw_files:
            file.unlink()

    tp_in_path = output_base / "tp"
    tp_out_path = tp_in_path / f"tp_2p0_{date}.nc"
    if tp_out_path.exists():
        logging.info(f"{tp_out_path} already exists. Skipping tp merging.")
    else:
        logging.info("Merging tp")
        tp_files = list(tp_in_path.glob(intermediate_pattern))
        tp = xr.open_mfdataset(tp_files, engine="h5netcdf")
        tp.to_netcdf(tp_out_path)
        tp.close()
        logging.info("Removing intermediate files")
        for file in tp_files:
            file.unlink()

    # ── NEW ───────────────────────────────────────────────────────────────────
    tp_0p25_out_path = tp_in_path / f"tp_0p25_{date}.nc"
    if tp_0p25_out_path.exists():
        logging.info(f"{tp_0p25_out_path} already exists. Skipping 0.25° tp merging.")
    else:
        logging.info("Merging 0.25° India TP")
        tp_0p25_pattern = f"*_{date}_0p25_INTERMEDIATE_3.nc"
        tp_0p25_files = list(tp_in_path.glob(tp_0p25_pattern))
        tp_0p25 = xr.open_mfdataset(tp_0p25_files, engine="h5netcdf")
        tp_0p25.to_netcdf(tp_0p25_out_path)
        tp_0p25.close()
        logging.info("Removing 0.25° intermediate files")
        for file in tp_0p25_files:
            file.unlink()
    # ─────────────────────────────────────────────────────────────────────────


REGION_HANDLERS = {
    "india": post_process_india,
    "ethiopia": post_process_ethiopia,
}


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
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

    raw_path = RAW_OUTPUT_BASE / f"{date}.zarr"
    ds = preprocess(xr.open_zarr(raw_path))

    targets = [args.region] if args.region else list(REGION_HANDLERS)
    for region in targets:
        logging.info(f"Running {region} post-processing")
        REGION_HANDLERS[region](ds, date)


if __name__ == "__main__":
    main()