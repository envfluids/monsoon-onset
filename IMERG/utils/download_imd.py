from datetime import datetime, timedelta
import os
import numpy as np
import pandas as pd
import logging
from pathlib import Path
import imdlib as imd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
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
    print(f"Fetching data for: {date_str}")
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
    except Exception as e:
        print(f"Failed on {date_str}: {e}")

if __name__ == "__main__":
    get_imd_data()