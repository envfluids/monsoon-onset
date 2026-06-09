import argparse
from datetime import datetime, timedelta
import os
import subprocess
import numpy as np
import pandas as pd
import logging
from pathlib import Path
import imdlib as imd

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

CDO_PATH = "/net/scratch2/marchakitus/conda-envs/operational/bin/cdo"

# Note: IMD data is valid for the previous
# 24 hours UTC relative to the requested date
# (ie. IMD of 6/3 counts for 6/2)

def get_imd_data(date_str=None):
    base = Path(__file__).resolve().parent.parent
    if date_str is None:
        date = datetime.today() - timedelta(days=1)  # Default to yesterday's date
        date_str = date.strftime('%Y%m%d')
    else:
        date = datetime.strptime(date_str, '%Y%m%d')

    date_p1 = date + timedelta(days=1)
    date_IMD_formatted = date_p1.strftime('%Y-%m-%d')
    var_type = 'rain'
    file_dir= base / "raw" / "IMD"
    file_dir = str(file_dir)
    try:
        logging.info(f"Fetching IMD data for date: {date_str}")
        data = imd.get_real_data(var_type, date_IMD_formatted, date_IMD_formatted, file_dir)
        ds = data.get_xarray()
        ds["rain"] = ds["rain"].where(ds["rain"] != -999, np.nan)
        ds['time'] = ds['time'] - pd.Timedelta(days=1)
        filename = f"{date_str}.nc4"
        filename_2 = f"regrid_{date_str}.nc4"
        out_path = os.path.join(file_dir, filename)
        out_path_2 = os.path.join(file_dir,filename_2)
        ds.to_netcdf(out_path) 
        regrid_file = base.parent / "AIFS" / "grids" / "grid_2p0.txt"
        logging.info(f"Regridding IMD data for date: {date_str} using {regrid_file} grid")
        command = [
            CDO_PATH,
            "-s",
            f"-remapcon,{regrid_file}",
            "-setmisstonn",
            out_path,
            out_path_2,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout:
            logging.debug("CDO stdout: %s", result.stdout.strip())
        if result.stderr:
            logging.debug("CDO stderr: %s", result.stderr.strip())
        logging.info(f"IMD data for date {date_str} saved to {out_path_2}")
    except subprocess.CalledProcessError as e:
        logging.error(
            "Failed to regrid IMD data for date %s. CDO exited with code %s.",
            date_str,
            e.returncode,
        )
        if e.stdout:
            logging.error("CDO stdout: %s", e.stdout.strip())
        if e.stderr:
            logging.error("CDO stderr: %s", e.stderr.strip())
        raise
    except Exception as e:
        logging.exception("Failed on %s: %s", date_str, e)
        raise

def main():
    parser = argparse.ArgumentParser(description="Download the latest IMD data.")
    parser.add_argument("--date", help="Date for which to download data (YYYYMMDD)")
    args = parser.parse_args()
    logging.info("Starting IMD data download process...")
    if args.date:
        # If a date is provided, use it
        get_imd_data(date_str=args.date)
    else:
        # Otherwise, get the latest data
        get_imd_data()

if __name__ == "__main__":
    main()
