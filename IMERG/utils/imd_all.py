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
    file_dir='/glade/derecho/scratch/marchakitus/monsoon-onset/IMERG/raw/IMD'
    date_f = date.strftime('%Y%m%d')
    try:
        data = imd.get_real_data(var_type, date_str, date_str, file_dir)
        ds = data.get_xarray()
        ds["rain"] = ds["rain"].where(ds["rain"] != -999, np.nan)
        filename = f"{date_f}.nc4"
        out_path = os.path.join(file_dir, filename)
        ds.to_netcdf(out_path) 

        regrid_file = "/glade/derecho/scratch/marchakitus/monsoon-onset/AIFS/grids/grid_2p0.txt"
        command = [
                "cdo",
                "-s",
                f"-remapcon,{regrid_file}",
                "-setmisstonn",
                f"{out_path}",
                f"/glade/derecho/scratch/marchakitus/monsoon-onset/IMERG/raw/IMD/regrid_{date_f}.nc4",
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