from datetime import datetime, timedelta
import os
import numpy as np
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

def get_imd_data(date):
    base = Path(__file__).resolve().parent.parent
    date_str = date.strftime('%Y-%m-%d')
    date_IMD_formatted = date.strftime('%Y%m%d')
    print(f"Fetching data for: {date_str}")
    var_type = 'rain'
    file_dir= base / "raw" / "IMD"
    file_dir = str(file_dir)
    try:
        logging.info(f"Fetching IMD data for date: {date_str}")
        data = imd.get_real_data(var_type, date_str, date_str, file_dir)
        ds = data.get_xarray()
        ds["rain"] = ds["rain"].where(ds["rain"] != -999, np.nan)
        filename = f"{date_IMD_formatted}.nc4"
        filename_2 = f"regrid_{date_IMD_formatted}.nc4"
        out_path = os.path.join(file_dir, filename)
        out_path_2 = os.path.join(file_dir,filename_2)
        ds.to_netcdf(out_path) 
        regrid_file = base.parent / "AIFS" / "grids" / "grid_2p0.txt"
        command = [
                CDO_PATH,
                "-s",
                f"-remapcon,{regrid_file}",
                "-setmisstonn",
                f"{out_path}",
                f"{out_path_2}",
            ]
        command = " ".join(command)
        os.system(command)
        return date_IMD_formatted
    except Exception as e:
        print(f"Failed on {date_str}: {e}")
        return None

if __name__ == "__main__":
    logging.info("Starting IMERG data download process...")
    # Define base_dir for testing if __file__ is not available (e.g. running selection in IDE)

    start_date = datetime(2026, 3, 1)
    end_date = datetime(2026, 3, 31)

    while start_date <= end_date:
        downloaded_date = get_imd_data(start_date)
        if downloaded_date:
            logging.info(f"New data downloaded for date: {downloaded_date}")
        start_date += timedelta(days=1)

    if downloaded_date:
        logging.info(f"New data downloaded for date: {downloaded_date}")
    else:
        logging.info("No new data downloaded. Exiting process.")
