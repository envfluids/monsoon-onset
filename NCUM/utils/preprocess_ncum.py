"""
preprocess_ncum.py – Converts NCUM NetCDF to the format expected by process_forecast.py.

Renames variables and dimensions to match AIFS conventions:
    forecast_reference_time → time
    latitude                → lat
    longitude               → lon
    forecast_period         → day
    time_24hr               → day (dimension)

Usage
-----
    python preprocess_ncum.py --20260504T00
"""

import re
from pathlib import Path
import xarray as xr
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


def preprocess_ncum(date: str, base: Path):
    input_path  = base / "raw" / "precipitation_amount" / f"precipitation_amount_{date}.nc"
    output_path = base / "output" / "precipitation_amount" / f"precipitation_amount_{date}.nc"

    if not input_path.exists():
        raise FileNotFoundError(f"NCUM file not found: {input_path}")

    # ── Ensure output directory exists ────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logging.info(f"Loading {input_path}")
    ds = xr.open_dataset(input_path, decode_timedelta=False)

    # ── Rename dimensions and coordinates ─────────────────────────────────────
    rename_map = {}

    if "forecast_reference_time" in ds.dims or "forecast_reference_time" in ds.coords:
        rename_map["forecast_reference_time"] = "time"
    if "latitude" in ds.dims or "latitude" in ds.coords:
        rename_map["latitude"] = "lat"
    if "longitude" in ds.dims or "longitude" in ds.coords:
        rename_map["longitude"] = "lon"
    if "time_24hr" in ds.dims or "time_24hr" in ds.coords:
        rename_map["time_24hr"] = "day"
    if "forecast_period" in ds.coords:
        rename_map["forecast_period"] = "day_values"

    ds = ds.rename(rename_map)

    # ── Convert forecast_period from hours to days ────────────────────────────
    if "day_values" in ds:
        day_in_hours = ds["day_values"].values
        ds["day"] = ("day", (day_in_hours / 24).astype(int))
        ds = ds.drop_vars("day_values")

    # ── Rename realization → number (ensemble dimension) ──────────────────────
    if "realization" in ds.dims or "realization" in ds.coords:
        ds = ds.rename({"realization": "number"})

    # ── Drop variables not needed by process_forecast.py ─────────────────────
    drop_vars = ["latitude_longitude", "time_24hr_bnds", "forecast_period_bnds"]
    for v in drop_vars:
        if v in ds:
            ds = ds.drop_vars(v)

    # ── Reorder dimensions to match expected order: ───────────────────────────
    # [day, number, time, lat, lon]
    ds["precipitation_amount"] = ds["precipitation_amount"].transpose(
        "day", "number", "time", "lat", "lon"
    )

    # ── Write directly to output path ─────────────────────────────────────────
    logging.info(f"Saving to {output_path}")
    ds.to_netcdf(output_path)
    ds.close()

    logging.info(f"Saved output to {output_path}")
    logging.info("Done.")


def main():
    # --- Check argument ---
    parser = argparse.ArgumentParser(description="Preprocess NCUM NetCDF file.")
    parser.add_argument(
        "--date",
        help="Forecast cycle in YYYYMMDDT00 format, for example 20260512T00.",
    )
    args = parser.parse_args()
    date = args.date

    if date is not None and not re.fullmatch(r"^\d{8}T00$", date):
        parser.error("date must use YYYYMMDDT00 format, for example 20260512T00")

    base = Path(__file__).resolve().parent.parent
    preprocess_ncum(date, base)


if __name__ == "__main__":
    main()