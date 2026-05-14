import earthkit.data as ekd
import datetime
import logging
import xarray as xr
import numpy as np
from pathlib import Path
import argparse

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


SST_DIR = Path(__file__).parent.parent / "raw" / "sst_ic"

def get_mars_retrieval(date, param):

    request = {
        "class": "od",
        "date": date.strftime("%Y-%m-%d"),
        "expver": "1",
        "param": param,
        "step": 0,
        "stream": "oper",
        "time": date.strftime("%H:%M:%S"),
        "type": "fc",
        "grid": "0.25/0.25",
        "REPRES": "LL",
    }


    levtype = "sfc"
    request["levtype"] = levtype

    print(request)
    
    return request

PARAM_SFC_MARS = {"sst": "sea_surface_temperature"}


def get_open_data(DATE, param):
    # Get the data for the current date and the previous date
    data_list = []
    for date in [DATE - datetime.timedelta(hours=12), DATE]:
        date_mars = date - datetime.timedelta(hours=24)
        data = ekd.from_source(
            "mars", request=get_mars_retrieval(date_mars, param)
        )
        data = data.to_xarray()
        if "time" not in data.coords:
            data = data.expand_dims("time")
            data["time"] = [np.datetime64(date)]
        if data.longitude.max().item() < 200:
            data = data.assign_coords(longitude=data.longitude % 360)
            data = data.sortby("longitude")


        data_list.append(data)
        
    ds = xr.concat(data_list, dim="time")

    return ds

def get_sst(date_f):
    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
    sfc_mars = get_open_data(DATE=date, param=list(PARAM_SFC_MARS.keys()))
    sfc_mars = sfc_mars.rename(PARAM_SFC_MARS)

    for var in sfc_mars.data_vars:
        sfc_mars[var] = sfc_mars[var].astype(np.float32)
        sfc_mars[var].attrs.pop("_earthkit")

    sfc_mars.to_netcdf(SST_DIR / f"sst_{date_f}.nc")

def main():
    parser = argparse.ArgumentParser(description='Run GenCast for a given date.')
    parser.add_argument('--date', type=str, help='Date to run the model for (YYYYMMDDTHH format)', required=True)
    args = parser.parse_args()
    date_f = args.date

    get_sst(date_f)

if __name__ == "__main__":
    main()