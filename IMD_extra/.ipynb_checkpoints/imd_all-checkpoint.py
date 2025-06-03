import xarray as xr
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.colors as mcolors
from matplotlib.colors import ListedColormap
from matplotlib.patches import Polygon
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from datetime import datetime, timedelta
import os
import glob
from datetime import datetime
import imdlib as imd



# Create date range from April 1 to today
start_date = datetime(datetime.today().year, 4, 1)
end_date = datetime.today()

# Generate all dates in range
dates = pd.date_range(start=start_date, end=end_date)
dates



# cdo sinfo 
for date in dates:
    date_str = date.strftime('%Y-%m-%d')
    print(f"Fetching data for: {date_str}")
    var_type = 'rain'
    file_dir='/global/cfs/cdirs/m3310/tyang25/IMD_cur'
    cdo_path = "/global/homes/t/tyang25/miniconda3/envs/IMDTest/bin/cdo"
    try:
        data = imd.get_real_data(var_type, date_str, date_str, file_dir)
        ds = data.get_xarray()
        ds["rain"] = ds["rain"].where(ds["rain"] != -999, np.nan)
        filename = f"rain_{date_str}.nc4"
        filename_2 = f"fixed_rain_{date_str}.nc4"
        out_path = os.path.join(file_dir, filename)
        out_path_2 = os.path.join(file_dir,filename_2)
        ds.to_netcdf(out_path) 
        # command = [
        #     cdo_path,
        #     f"-setmissval,1e20",
        #     out_path,
        #     out_path_2,
        # ]
        # command = " ".join(command)
        # os.system(command)
        # Need to possibly use CDO as regridder
        regrid_file = "/global/homes/t/tyang25/Indian_Monsoon_Onset/Targetgrid.txt"
        command = [
                cdo_path,
                "-s",
                f"-remapcon,{regrid_file}",
                "-setmisstonn",
                f"{out_path}",
                f"/global/cfs/cdirs/m3310/tyang25/IMD_cur/regrid_rain_{date_str}.nc4",
            ]
        command = " ".join(command)
        os.system(command)
        # command = [
        #     cdo_path,
        #     "sinfo",
        #     f"/global/cfs/cdirs/m3310/tyang25/IMD_cur/regrid_rain_{date_str}.nc4",
        # ]
        # command = " ".join(command)
        # os.system(command)
    except Exception as e:
        print(f"Failed on {date_str}: {e}")