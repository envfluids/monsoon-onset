import numpy as np
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
import os
import glob
import geopandas as gpd
from shapely.geometry import box
from matplotlib.cm import ScalarMappable
from pathlib import Path
import logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
)

def get_IMERG(IMERG_data_dir):
    file_pattern = os.path.join(IMERG_data_dir, '*2025*.nc4')
    nc_files = sorted(glob.glob(str(file_pattern)))

    filtered_files = []
    for file in nc_files:
        basename = os.path.basename(file)
        try:
            date_str = basename.split('.')[4][:8] 
            if int(date_str) >= 20250401:
                filtered_files.append(file)
        except:
            continue

    dat = xr.open_mfdataset(filtered_files, combine='by_coords')
    dat = dat.sel(time = dat['time'][-5:].values)['precipitation']
    dat_ap = dat.sel(lat= slice(7, 39), lon=slice(67, 101)).coarsen(lat=20, lon=20, boundary="trim").mean()
    
    return dat_ap

def get_model_data(base):
    IMERG_data_dir = base / "IMERG" / "raw" / "IMERG_daily"
    dat_ap = get_IMERG(IMERG_data_dir)

    date_times = dat_ap.time.values
    last_dt = pd.to_datetime(date_times[-1])
    target_dt = last_dt - pd.Timedelta(days=5)
    fname = target_dt.strftime('tp_%Y%m%dT%H.nc')
    dateF = last_dt.strftime('%Y%m%d')

    NGCM_DATA_PATH = base / "NeuralGCM" / "output" / "tp" / fname
    AIFS_DATA_PATH = base / "AIFS" / "output" / "tp" / fname
    df_ngcm = xr.open_dataset(NGCM_DATA_PATH)
    df_AIFS = xr.open_dataset(AIFS_DATA_PATH)
    
    dat_ap = dat_ap.sum("time")
    days = np.arange(0,5,1)
    df_ngcm = df_ngcm.sel(day = days)
    df_AIFS = df_AIFS.sel(day = days)
    df_ngcm = df_ngcm.sum("day")
    df_ngcm = df_ngcm.mean("number")
    df_AIFS = df_AIFS.sum("day")

    df_AIFS = df_AIFS.squeeze("time", drop=True)
    df_ngcm = df_ngcm.squeeze("time", drop=True)

    dat_ap = dat_ap.transpose("lat","lon")
    ds_D = xr.Dataset(
        data_vars={
            "tp_IMERG": (("lat", "lon"), dat_ap.values.astype(np.float32))
        },
        coords={
            "lat": dat_ap.lat.values.astype(np.float32),
            "lon": dat_ap.lon.values.astype(np.float32)
        }
    )
    ds_D['tp_AIFS'] = df_AIFS['tp']
    ds_D['tp_ngcm'] = df_ngcm['tp']
    ds_D['tp_ngcm_Bias'] = ds_D['tp_ngcm'] - ds_D['tp_IMERG']
    ds_D['tp_AIFS_Bias'] = ds_D['tp_AIFS'] - ds_D['tp_IMERG']

    return ds_D, date_times, dateF

def plot_IMERG_model_bias(PATHS, ds_D, For_date_times):
    ONSET_CSV, SHAPEFILE, OUTPUT_PATH = PATHS
    OUTPUT_PATH_BIAS, OUTPUT_PATH_MAG = OUTPUT_PATH
    df_meta = (
        pd.read_csv(ONSET_CSV)[['lat', 'lon', 'onset_thresh']]
        .drop_duplicates(subset=['lat', 'lon'])
    )

    # build 2°×2° highlight boxes around each (lat,lon)
    boxes = [
        box(lon - 1, lat - 1, lon + 1, lat + 1)
        for lon, lat in zip(df_meta.lon, df_meta.lat)
    ]
    highlight_gdf = gpd.GeoDataFrame(geometry=boxes, crs="EPSG:4326")

    # load India boundary
    india = gpd.read_file(SHAPEFILE).to_crs("EPSG:4326")
    minx, miny, maxx, maxy = india.total_bounds
    xticks = np.arange(np.floor(minx), np.ceil(maxx) + 1, 2)
    yticks = np.arange(np.floor(miny), np.ceil(maxy) + 1, 2)

    # build mask of df_meta grid points
    lons = ds_D.lon.values
    lats = ds_D.lat.values
    lon2d, lat2d = np.meshgrid(lons, lats)
    mask2d = np.zeros_like(lon2d, dtype=bool)
    for _, row in df_meta.iterrows():
        mask2d |= (
            np.isclose(lat2d, row.lat) &
            np.isclose(lon2d, row.lon)
        )

    # --- PLOT SETUP ---
    vars_cmaps = [
        ("tp_IMERG",      "YlGnBu",  "IMERG"),
        ("tp_ngcm_Bias",  "seismic", "NGCM Bias"),
        ("tp_AIFS_Bias",  "seismic", "AIFS Bias")
    ]

    fig, axes = plt.subplots(1, 3, figsize=(12, 6))

    for ax, (var, cmap, leg) in zip(axes, vars_cmaps):
        da = ds_D[var]
        da_masked = da.where(mask2d)
        

        # set color limits
        if "Bias" in var:
            vmin, vmax = -60, 60
        else:
            vmin, vmax = float(da_masked.min()), float(da_masked.max())

        # plot the masked field
        im = da_masked.plot.pcolormesh(
            ax=ax,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            add_colorbar=False,
            zorder=1
        )

        # overlay India border on top
        india.plot(
            ax=ax,
            facecolor="none",
            edgecolor="black",
            linewidth=1,
            zorder=2
        )

        # overlay highlight boxes
        highlight_gdf.plot(
            ax=ax,
            facecolor="none",
            edgecolor="magenta",
            linewidth=1,
            zorder=3
        )

        # add horizontal colorbar
        cbar = fig.colorbar(
            im,
            ax=ax,
            orientation='horizontal',
            fraction=0.046,
            pad=0.08
        )
        cbar.set_label("Rainfall (mm)", fontsize=12)
        cbar.ax.tick_params(labelsize=10)
        
        # increase tick density on the bias plots
        if "tp_IMERG" in var:
            vmin, vmax = float(da_masked.min()), float(da_masked.max())
            cb_ticks = None
        else:
            ticks = np.arange(vmin, vmax+1, 10)
            cbar.set_ticks(ticks)
            cbar.set_ticklabels([f"{t:.0f}" for t in ticks])
        #else:
        #    vmin, vmax = float(da_masked.min()), float(da_masked.max())
        #    cb_ticks = None

        # decorate
        ax.set_title(leg, fontsize=12)
        ax.set_xticks(xticks)
        ax.set_yticks(yticks)
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.set_xlabel("Longitude", fontsize=9)
        ax.set_ylabel("Latitude", fontsize=9)
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.tick_params(labelsize=8)

    date_start = np.datetime_as_string(For_date_times[0], unit='D')
    date_end   = np.datetime_as_string(For_date_times[-1], unit='D')
    date_range = f"{date_start} to {date_end}"

    plt.tight_layout()
    plt.suptitle(
        f"Dissemination Grids IMERG Precipitation & Model Bias over India ({date_range})",
        y=0.92,
        fontsize=12
    )


    plt.savefig(OUTPUT_PATH_BIAS, dpi=300, bbox_inches="tight")
    plt.close()
    ######################

    vars_info = [
        ("tp_IMERG",     "YlGnBu", "IMERG"),
        ("tp_ngcm",      "YlGnBu", "NGCM"),
        ("tp_AIFS",      "YlGnBu", "AIFS")
    ]
    masked_arrays = [ ds_D[v].where(mask2d) for v,_,_ in vars_info ]

    # 1) compute a **single** vmin/vmax across all three
    global_min = float(min(da.min().values for da in masked_arrays))
    global_max = float(max(da.max().values for da in masked_arrays))
    # --- setup figure & axes ---
    fig, axes = plt.subplots(1, 3, figsize=(12, 6))

    # 2) plot each with the **same** Normalize
    norm = Normalize(vmin=global_min, vmax=global_max)

    for ax, (var, cmap, title), da_masked in zip(axes, vars_info, masked_arrays):
        # pcolormesh with shared norm
        im = da_masked.plot.pcolormesh(
            ax=ax,
            cmap=cmap,
            norm=norm,
            add_colorbar=False,
            zorder=1
        )

        # overlay India & highlight boxes (as before)…
        india.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=1, zorder=2)
        highlight_gdf.plot(ax=ax, facecolor="none", edgecolor="magenta", linewidth=1, zorder=3)

        ax.set_title(title, fontsize=12)
        ax.set_xticks(xticks); 
        ax.set_yticks(yticks)
        ax.set_xlim(minx, maxx); ax.set_ylim(miny, maxy)
        ax.set_xlabel("Longitude", fontsize=9)
        ax.set_ylabel("Latitude", fontsize=9)
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.tick_params(labelsize=8)

    # 3) single, bottom colorbar
    sm = ScalarMappable(norm=norm, cmap="YlGnBu")
    sm.set_array([])  # dummy for colorbar
    cbar = fig.colorbar(
        sm,
        ax=axes,
        orientation='horizontal',
        fraction=0.05,
        pad=0.1,
        ticks=np.linspace(global_min, global_max, 10)  # e.g. 6 ticks
    )
    ticks=np.linspace(global_min, global_max-2, 10)
    cbar.set_ticks(ticks)
    cbar.set_ticklabels([f"{t:.0f}" for t in ticks])

    cbar.set_label("Rainfall (mm)", fontsize=12)
    cbar.ax.tick_params(labelsize=10)

    # supertitle with date range
    date_start = np.datetime_as_string(For_date_times[0], unit='D')
    date_end   = np.datetime_as_string(For_date_times[-1], unit='D')
    plt.suptitle(
        f"Dissemination Grids IMERG, NGCM, and AIFS Precipitation over India ({date_start} to {date_end})",
        y=0.83, fontsize=12
    )


    #plt.tight_layout()
    plt.savefig(OUTPUT_PATH_MAG, dpi=300, bbox_inches="tight")
    plt.close()
    logging.info(f"Saved model bias plots to {OUTPUT_PATH_BIAS} and {OUTPUT_PATH_MAG}")

def main():
    base = Path(__file__).resolve().parent.parent.parent
    logging.info(f"Getting model data")
    ds_D, date_times, dateF = get_model_data(base)
    logging.info(f"Got model data for final date {dateF}")
    SHAPEFILE   = base / "blend" / "data" / "india_shapefile" / "India_Country_Boundary.shp"
    ONSET_CSV   = base / "blend" / "data" / "support" / "onsets_2024.csv"
    OUTPUT_DIR = base / "IMERG" / "output" / dateF
    OUTPUT_BIAS_FILE = OUTPUT_DIR / "IMERG_NGCM_AIFS_bias.png"
    OUTPUT_MAG_FILE = OUTPUT_DIR / "IMERG_NGCM_AIFS_magnitude.png"

    logging.info(f"Plotting model bias")
    OUTPUT_PATHS = (OUTPUT_BIAS_FILE, OUTPUT_MAG_FILE)
    PATHS = ONSET_CSV, SHAPEFILE, OUTPUT_PATHS
    plot_IMERG_model_bias(PATHS, ds_D, date_times)
    logging.info(f"Finished plotting model bias")

if __name__ == "__main__":
    main()
    logging.info("plot_bias completed successfully.")