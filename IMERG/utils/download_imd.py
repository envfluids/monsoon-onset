from datetime import datetime
import os
import numpy as np
import logging
from pathlib import Path
import imdlib as imd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

def get_imd_data():
    base = Path(__file__).resolve().parent.parent
    date = datetime.today()
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
                "cdo",
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