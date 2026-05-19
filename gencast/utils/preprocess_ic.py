import datetime
import logging
from pathlib import Path

import earthkit.data as ekd
import numpy as np
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


BASE = Path(__file__).parent.parent
REPO_ROOT = BASE.parent

GRIB_OUTPUT_DIR = REPO_ROOT / "AIFS" / "raw" / "ifs_ic" / "grib"
SST_DIR = BASE / "raw" / "sst_ic"
LSM_PATH = BASE / "data" / "gencast_lsm_mask.nc"
LSM_MASK = xr.open_dataarray(LSM_PATH, engine="h5netcdf")


def get_grib_path(date):
    date_f = date.strftime("%Y%m%d%H%M%S")

    grib_path = GRIB_OUTPUT_DIR / f"{date_f}-0h-oper-fc.grib2"
    if not grib_path.exists():
        logging.error(f"GRIB file {grib_path} does not exist.")
        raise FileNotFoundError(f"GRIB file {grib_path} not found.")

    return str(grib_path)


def get_sst(date):
    date_f = date.strftime("%Y%m%dT%H")
    sst_path = SST_DIR / f"sst_{date_f}.nc"
    if not sst_path.exists():
        logging.error(f"SST file {sst_path} does not exist.")
        raise FileNotFoundError(f"SST file {sst_path} not found.")
    sst = xr.open_dataset(sst_path, engine="h5netcdf")
    return sst


def get_open_data(DATE, param, levelist=[]):
    # Get the data for the current date and the previous date
    data_list = []
    for date in [DATE - datetime.timedelta(hours=12), DATE]:
        full_data_path = get_grib_path(date)
        full_data = ekd.from_source("file", path=full_data_path)
        if levelist:
            data = full_data.sel(param=param, level=levelist)
        else:
            data = full_data.sel(param=param, levtype="sfc")

        data = data.to_xarray()
        if "time" not in data.coords:
            data = data.expand_dims("time")
            data["time"] = [np.datetime64(date).astype("datetime64[ns]")]
        if data.longitude.max().item() < 200:
            data = data.assign_coords(longitude=data.longitude % 360)
            data = data.sortby("longitude")

        data_list.append(data)

    ds = xr.concat(data_list, dim="time")

    return ds


def get_ic(date):

    PARAM_SFC = {
        "10u": "10m_u_component_of_wind",
        "10v": "10m_v_component_of_wind",
        "2t": "2m_temperature",
        "msl": "mean_sea_level_pressure",
        "lsm": "land_sea_mask",
        "z": "geopotential_at_surface",
    }
    PARAM_PL = {
        "gh": "geopotential_height",
        "t": "temperature",
        "u": "u_component_of_wind",
        "v": "v_component_of_wind",
        "w": "vertical_velocity",
        "q": "specific_humidity",
    }
    LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50]

    sfc = get_open_data(DATE=date, param=list(PARAM_SFC.keys()))
    sfc = sfc.rename(PARAM_SFC)
    sst = get_sst(date)
    pl = get_open_data(DATE=date, param=list(PARAM_PL.keys()), levelist=LEVELS)
    pl = pl.rename(PARAM_PL)

    ic = xr.merge([sfc, pl, sst], join="exact")

    ic = ic.rename({"latitude": "lat", "longitude": "lon"})
    ic["lat"] = ic.lat.astype(np.float32)
    ic["lon"] = ic.lon.astype(np.float32)
    ic["level"] = ic.level.astype(np.int32)
    ic = ic.sortby(["lat", "lon"])

    ic["sea_surface_temperature"] = ic["sea_surface_temperature"].where(LSM_MASK == 1)

    ic["geopotential_height"] = ic["geopotential_height"] * 9.80665
    ic = ic.rename({"geopotential_height": "geopotential"})

    for var in ic.data_vars:
        ic[var] = ic[var].astype(np.float32)
        if "_earthkit" in ic[var].attrs:
            ic[var].attrs.pop("_earthkit")

    ic = ic.expand_dims("batch")
    ic = ic.assign_coords(datetime=(("batch", "time"), [ic.time.values]))

    ic["geopotential_at_surface"] = (
        ic["geopotential_at_surface"].isel(time=0, batch=0).squeeze()
    )
    ic["land_sea_mask"] = ic["land_sea_mask"].isel(time=0, batch=0).squeeze()

    return ic
