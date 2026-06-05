"""
Download and process the ERA5 climatology for Africa

It streams the public WeatherBench-2 hourly climatology from Google Cloud Storage, 
keeps only the variables / levels / region the plots need, reduces it to a 
compact day-of-year climatology.

Usage: python build_climatology.py
"""
from datetime import datetime, timezone
from pathlib import Path
import xarray as xr

# ── SOURCE / OUTPUT CONFIG ──────────────────────────────────────────────────────

# dims: (hour=4, dayofyear=366, level=13, latitude=721, longitude=1440)
DEFAULT_SOURCE_URL = (
    "gs://weatherbench2/datasets/era5-hourly-climatology/1990-2019_6h_1440x721.zarr"
)
CLIM_PERIOD = "1990-2019"
DEFAULT_OUTPUT = Path("climatology") / f"era5_clim_africa_{CLIM_PERIOD}.zarr"
U_VAR = "u_component_of_wind"
V_VAR = "v_component_of_wind"
MSL_VAR = "mean_sea_level_pressure"
DEFAULT_PRECIP_VAR = "total_precipitation_24hr"
WIND_LEVELS = (850, 200)
# Africa domain
REGION = {"lon": (-26.0, 73.0), "lat": (-56.0, 40.0)}
PAD = 1.0
# Chunking: split both the day-of-year axis and the grid 
# These divide evenly: 6 x 3 x 3 = 54 chunks
CHUNKS = {"dayofyear": 61, "lat": 131, "lon": 135}


# ── PROCESSING ──────────────────────────────────────────────────────────────────

def open_source(url: str) -> xr.Dataset:
    """Lazily open the WB2 climatology store (dask-backed, anonymous read)."""
    return xr.open_zarr(url, storage_options={"token": "anon"}, chunks={})

def build_climatology(src: xr.Dataset, levels=WIND_LEVELS, region=REGION, precip_var: str = DEFAULT_PRECIP_VAR) -> xr.Dataset:
    """
    Reduce the global hourly climatology to a compact regional day-of-year
    climatology.
    """
    lon_min, lon_max = region["lon"]
    lat_min, lat_max = region["lat"]

    src = src.rename({"latitude": "lat", "longitude": "lon"})
    src = src.assign_coords(lon=(((src.lon + 180) % 360) - 180)).sortby("lon")
    src = src.sortby("lat")
    src = src.sel(lat=slice(lat_min - PAD, lat_max + PAD), lon=slice(lon_min - PAD, lon_max + PAD))

    def daily(da: xr.DataArray) -> xr.DataArray:
        return da.mean("hour")

    out = xr.Dataset(coords={"dayofyear": src.dayofyear, "lat": src.lat, "lon": src.lon})

    u, v = src[U_VAR], src[V_VAR]
    for lev in levels:
        out[f"u_{lev}"] = daily(u.sel(level=lev, drop=True))
        out[f"v_{lev}"] = daily(v.sel(level=lev, drop=True))
        out[f"u_{lev}"].attrs = {"units": "m s-1"}
        out[f"v_{lev}"].attrs = {"units": "m s-1"}

    # Pa to hPa
    out["msl"] = daily(src[MSL_VAR]) / 100.0  
    out["msl"].attrs = {"units": "hPa"}

    out["tp"] = daily(src[precip_var]) * 1000.0
    out["tp"].attrs = {"units": "mm/day"}

    # Minimal coordinate attributes.
    out["lat"].attrs = {"units": "degrees_north"}
    out["lon"].attrs = {"units": "degrees_east"}
    out["dayofyear"].attrs = {}
    out = out.drop_vars("level", errors="ignore")

    out.attrs = {
        "source": DEFAULT_SOURCE_URL,
        "climatology_period": CLIM_PERIOD,
        "precip_source_variable": precip_var,
        "region": f"lat {lat_min}..{lat_max}, lon {lon_min}..{lon_max} (pad {PAD})",
        "hour_reduction": "mean over 4 synoptic hours (00/06/12/18Z)",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_by": "build_climatology.py",
    }
    return out

def write_climatology(out: xr.Dataset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Drop encoding inherited from the source store
    for var in (*out.data_vars.values(), *out.coords.values()):
        var.encoding.clear()
    
    # Split day-of-year and the grid
    out = out.chunk(CHUNKS)
    out.to_zarr(path, mode="w", consolidated=True)

# ── CLI ─────────────────────────────────────────────────────────────────────────
def remake_climatology(output_path: Path = DEFAULT_OUTPUT):
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    src = open_source(DEFAULT_SOURCE_URL)
    out = build_climatology(src, precip_var=DEFAULT_PRECIP_VAR)
    write_climatology(out, output_path)

def main():
    src = open_source(DEFAULT_SOURCE_URL)
    out = build_climatology(src, precip_var=DEFAULT_PRECIP_VAR)
    write_climatology(out, DEFAULT_OUTPUT)

if __name__ == "__main__":
    main()