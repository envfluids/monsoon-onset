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
from pathlib import Path
import argparse
import logging
import json

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

def sequence_overlap(X, lseason, nday):
    nr, nv = np.shape(X)
    nyear = nr // lseason
    indice = []
    for i in range(nday):
        row = np.arange(i, lseason + i)
        indice.append(row)
    indice = np.array(indice)
    nseq, lseq = np.shape(indice)
    Y = np.zeros((lseq * nyear, nv * nday))
    for i in range(nyear):
        sample = X[i * lseason: (i + 1) * lseason]
        sample = np.vstack([np.tile(sample[0], (nday - 1, 1)), sample])
        sample1 = np.zeros((lseq, nday * nv))
        for j in range(nday):
            sample1[:lseq, (j * nv):(j + 1) * nv] = sample[indice[j], :nv]
        Y[(i * lseq) : (i + 1) *lseq,:nv*nday] = sample1
    return Y

def onset_agro_bis(X, lseason, defdry, sw, wet, sd, dry, window):
    N, C = np.shape(X)
    nyear = N // lseason
    #print("nyear", nyear)
    W = np.zeros(np.shape(X))
    W[X > defdry] = 1
    #print("W", W)
    swet = None # Have to add this line for python
    if sw > 1:
        swet = sequence_overlap(np.transpose([np.arange(lseason)]), lseason, sw)
        swet = np.transpose(swet[sw - 1:lseason,:])
        #print("After-pad swet", swet)
        #print(swet.reshape((-1, 1), order = 'F') @ np.ones((1, C)))
        #print(np.ones(((lseason - (sw - 1)) * sw, 1)) @ np.arange(0, lseason * C, lseason).reshape(1, -1))
        swet = (swet.reshape((-1, 1), order='F') @ np.ones((1, C))) + np.ones(((lseason - (sw - 1)) * sw, 1)) @ (np.arange(0, lseason * C, lseason).reshape(1, -1))
        #print("Before Reshape", swet)
        #print(C*(lseason - (sw - 1)))
        swet = swet.reshape((sw,C*(lseason-(sw-1))), order='F') #matlab reshape is column-major while numpy is row-major default
        #print("After Reshape", swet)
    sdry = None #Have to add this line for python
    if sd > 1:
        sdry = sequence_overlap(np.transpose([np.arange(lseason)]), lseason, sd)
        sdry = np.transpose(sdry[sd - 1:lseason,:])
        sdry = (sdry.reshape((-1, 1), order='F') @ np.ones((1, C))) + np.ones(((lseason - (sd - 1)) * sd, 1)) @ (np.arange(0, lseason * C, lseason).reshape(1, -1))
        sdry = sdry.reshape((sd,C*(lseason-(sd-1))), order='F')
    
    O1 = np.full((nyear, C), np.nan)
    O2 = np.full((nyear, C), np.nan)
    
    S = window - (sd - 1)
    S2 = sequence_overlap(np.transpose([np.arange(lseason)]), lseason, S)
    #print("S2", S2)
    S2 = np.transpose(S2[S - 1:lseason])
    #print("S2new", S2)
    Lw = lseason - (sw - 1)
    SWmean = np.zeros((nyear * Lw, C))
    for i in range(nyear):
        sample = X[(i * lseason): ((i + 1) * lseason), :]
        #print("sample", sample)
        #print("swet", swet)
        sample_flat = sample.ravel(order="F")
        #print("sample_flat", sample_flat)
        if sw > 1:
            #print(Lw, i)
            #print("Before sum", sample_flat[swet.astype(int)])
            #print("After sum", np.sum(sample_flat[swet.astype(int)], axis=0))
            SWmean[(i * Lw):(Lw * (i + 1)),:] = np.reshape(np.sum(sample_flat[swet.astype(int)], axis=0), (lseason - (sw - 1), C), order="F")
            #print("SWmean",  SWmean[(i * Lw):(Lw * (i + 1)),:], i)
        else:
            SWmean[(i * Lw):(Lw * (i + 1)),:] = sample
    #print(SWmean.shape)
    MWmean = np.zeros(C)
    for i in range(C):
        MWmean[i] = np.mean(SWmean[SWmean[:,i] > defdry, i]) 
        #Seems like MWmean is taking the mean of two days summed together 
        # in other words making sure that a full seqeunce of days passes the threshold defined in defdry
        # ex.) if the cumulative precipitation across sw days at a time (0, 1), (2, 3) is greater than the threshold defined in defdry - its counted. 
    # if wet == 0:
    #     wet = MWmean
    #     print(wet.shape)
    # else:
    wet = wet
    # #print(wet.shape)
    for i in range(nyear):
        sample = X[(i * lseason): ((i + 1) * lseason), :]
        wsample = W[(i * lseason): ((i + 1) * lseason), :]
        sample_flat = sample.ravel(order="F")
        SW = sample
        SD = sample
        if sw > 1:
            SW = np.reshape(np.sum(sample_flat[swet.astype(int)], axis=0), (lseason - (sw - 1), C), order= "F")
        if sd > 1:
            SD = np.reshape(np.sum(sample_flat[sdry.astype(int)], axis=0), (lseason - (sd - 1), C), order = "F")
        nrw, ncw = np.shape(SW)
        nrd, ncd = np.shape(SD)
        for j in range(C):
            #print("SW", SW[:,j])
            SW_extension = np.concatenate([SW[:,j], np.ones(sw - 1) * SW[lseason - sw, j]])
            SD_extension = np.concatenate([SD[:,j], np.zeros(sd - 1)])
            tab = np.column_stack([sample[:, j], wsample[:,j], SW_extension, SD_extension])
            if i == 123: print(f"Tab for year {i}, station {j}:", pd.DataFrame(tab).to_string())
            nrtab, nctab = np.shape(tab)
            o1 = np.where((tab[:, 2] >= wet[j]) & (tab[:, 1] == 1) & (np.arange(tab.shape[0]) >= 30))[0]
            #if i == 123: print(o1)
            D = tab[:, 3]
            #if (i == 81): print(D.shape)
            D = np.transpose(D[S2.astype(int)])
            #if (i == 81): print(D.shape)
            D = np.vstack([D, np.zeros((window - sd, S))])
            #if (i == 81): print(D.shape)
            if o1.size > 0:
                O1[i, j] = o1[0]
                tab2 = D[o1, :]
                #if(i == 5):
                    #print("tab2 shape",tab2.shape)
                    #print("tab2", tab2)
                    #print(np.min(np.transpose(tab2)))
                o2 = o1[np.min(tab2, axis = 1) > dry]
                if o2.size > 0:
                    #print("o2", o2)
                    O2[i, j] = o2[0]
    return O1, O2, MWmean

def plot_graph_mod(
    array, 
    title, 
    bounds,
    dat,
    fp,
    yesdate,
    res = False,
):
    b = bounds # Don't worry about this line - I need this to distinguish between different plots
    if bounds:
        colors = ['white', 'blue', 'cyan', 'green', 'yellowgreen', 'yellow', 'orange', 'orangered', 'red']
        cmap = mcolors.LinearSegmentedColormap.from_list('custom_colormap', colors, N=len(colors))
        norm = mcolors.BoundaryNorm(bounds, cmap.N)
    result = array
    if not res:
        result = xr.DataArray(
            array,
            coords={"LATITUDE": dat["lat"], "LONGITUDE": dat["lon"]},
            dims=["lat", "lon"],
            name="Onset"
        )
    
    
    fig, ax = plt.subplots(figsize=(15, 9), subplot_kw={"projection": ccrs.PlateCarree()})
    if (not b):
        im = result.plot(
            x='LONGITUDE', y='LATITUDE',
            ax=ax, transform=ccrs.PlateCarree(),
            cmap= ListedColormap(['lightgreen']),
            add_colorbar=False
        )
        for (i, j), z in np.ndenumerate(array):
            #rint("Here")
           
            if not np.isnan(z):
                date = (datetime(2025, 1, 1) + timedelta(z + 90)).strftime('%m/%d')
                lat = dat["lat"][i]
                lon = dat["lon"][j]
                ax.text(lon, lat, '{:s}'.format(str(date)), ha='center', va='center', transform=ccrs.PlateCarree(), fontsize=6, rotation=45, color='black', fontweight='bold')
    else:
        im = result.plot(
            x='LONGITUDE', y='LATITUDE',
            ax=ax, transform=ccrs.PlateCarree(),
            cmap=cmap,
            norm=norm,
            cbar_kwargs={'label': 'Rainfall [mm]'}
        )
        for (i, j), z in np.ndenumerate(array):
            if not np.isnan(z):
                lat = dat["lat"][i]
                lon = dat["lon"][j]
                ax.text(lon, lat, '{:.2f}'.format(float(z)), ha='center', va='center', transform=ccrs.PlateCarree(), fontsize=8, fontweight='bold', color='black')
    ax.coastlines()
    ax.add_feature(cfeature.BORDERS) 
    gridlines = ax.gridlines(draw_labels=True, linestyle="--", color="gray")
    ax.set_title(title)
   
    base = Path(__file__).resolve().parent.parent
    df_path = base / "data" / "grid_2x2_dissem.csv"
    df = pd.read_csv(df_path)
    df_1d = df[df["dissem33_15"] == 1]
    coords = list(df_1d[['lat', 'lon']].itertuples(index=False, name=None))
    marker_lats, marker_lons = zip(*coords)
    for lat_val, lon_val in coords:
        cell = [
            (lon_val - 1, lat_val - 1),
            (lon_val + 1, lat_val - 1),  
            (lon_val + 1, lat_val + 1),  
            (lon_val - 1, lat_val + 1),  
        ]
    
        polygon = Polygon(
            cell,
            facecolor='none',
            edgecolor='magenta',
            linewidth=4,
            transform=ccrs.PlateCarree()
        )
        ax.add_patch(polygon)

    if (b):
        cbar = im.colorbar
        cbar.ax.set_yticklabels(bounds)
    save_path = base / "output" / yesdate / fp
    plt.savefig(save_path, dpi=100, bbox_inches='tight')
    plt.close()
 
def get_onset_csv(O1, dat_ap, out_path):
    O1_name = np.array([[(datetime(2025,1,1) + timedelta(days=int(x) + 91)).strftime('%Y/%m/%d') if not np.isnan(x) else None for x in row] for row in O1])
    O1set = xr.DataArray(
            O1_name,
            coords={"lat": dat_ap["lat"].astype(int), "lon": dat_ap["lon"].astype(int)},
            dims=["lat", "lon"],
            name="Onset_Occurrence"
        )
    O1set = O1set.transpose("lon", "lat")
    odatepath = out_path / f'OnsetDates.csv'
    O1set.to_dataframe().to_csv(odatepath)

# Main Workflow:
def process(date_f):
    base = Path(__file__).resolve().parent.parent
    data_dir = base / "raw" / "IMERG_daily"
    file_pattern = os.path.join(data_dir, '*2025*.nc4')
    nc_files = sorted(glob.glob(file_pattern))
    #print(nc_files)
    filtered_files = []
    for file in nc_files:
        basename = os.path.basename(file)
        try:
            date_str = basename.split('.')[4][:8] 
            if int(date_str) >= 20250401:
                filtered_files.append(file)
        except:
            continue

    # yesdate = (datetime.now() - timedelta(days=1)).strftime('%Y%m%dT12')
    yesdate = date_f
    logging.info(f"Processing data for date: {yesdate}")
    output_dir = base / "output" / yesdate
    logging.info(f"Output directory: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)
    dat = xr.open_mfdataset(filtered_files, combine='by_coords')
    MWmean_dir = base / "data" / "MWmean.npy"
    mwmean = np.load(MWmean_dir)
    dat_ap = dat.sel(lat= slice(7, 39), lon=slice(67, 101)).coarsen(lat=20, lon=20, boundary="trim").mean()
    dat_5 = dat_ap.isel(time=slice(-5, None))
    dat_1 = dat_ap.isel(time=slice(-1, None))

    rainfall_5 = dat_5["precipitation"].mean(dim='time')
    rainfall_5 = np.transpose(np.where(rainfall_5 < 0, np.nan, rainfall_5.values))
    rainfall_1 = dat_1["precipitation"].mean(dim='time')
    rainfall_1 = np.transpose(np.where(rainfall_1 < 0, np.nan, rainfall_1.values))

    logging.info("Plotting graphs")

    O1, O2, MWmean = onset_agro_bis(dat_ap["precipitation"].stack(grid=["lat", "lon"]).values, dat_ap['time'].values.shape[0], 1, 5, mwmean, 10, 5, 30)
    O1 = O1[0].reshape((len(dat_ap["lat"]), len(dat_ap["lon"])))
    O2 = O2[0].reshape((len(dat_ap["lat"]), len(dat_ap["lon"])))
    plot_graph_mod(O1, "[IMERG] Onset Occurences - Without Dryspell", [], dat_ap, "Onset_occ_wo_dryspell", yesdate)
    plot_graph_mod(O2, "[IMERG] Onset Occurences - With Dryspell", [], dat_ap, "Onset_occ_w_dryspell", yesdate)  
    plot_graph_mod(rainfall_5, "5-day Rainfall Average", [0,1,2,3,4,5,6,7,8,9], dat_5, "Five_day_rain", yesdate)
    plot_graph_mod(rainfall_1, "1-day Rainfall Average", [0,1,2,3,4,5,6,7,8,9], dat_1, "One_day_rain", yesdate)

    get_onset_csv(O1, dat_ap, output_dir)
    logging.info("Graphs plotted successfully.")


def process_IMD_IMERG(date_f):
    #Step (1) Downloading the IMD Data: 
    base = Path(__file__).resolve().parent.parent
    data_dir = base / "raw" / "IMERG_daily"
    
    #Step(2) IMERG Whole Plots
    
    file_pattern = os.path.join(data_dir, '*2025*.nc4')
    nc_files = sorted(glob.glob(file_pattern))
    #print(nc_files)
    filtered_files = []
    for file in nc_files:
        basename = os.path.basename(file)
        try:
            date_str = basename.split('.')[4][:8] 
            if int(date_str) >= 20250401:
                filtered_files.append(file)
        except:
            continue
    yesdate = date_f
    logging.info(f"Processing IMERG data for date: {yesdate}")
    output_dir = base / "output" / yesdate
    logging.info(f"Output directory: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)
    dat = xr.open_mfdataset(filtered_files, combine='by_coords')
    MWmean_dir = base / "data" / "MWmean.npy"
    mwmean = np.load(MWmean_dir)
    dat_ap_IMERG = dat.sel(lat= slice(7, 39), lon=slice(67, 101)).coarsen(lat=20, lon=20, boundary="trim").mean()
    dat_5 = dat_ap_IMERG.isel(time=slice(-5, None))
    dat_1 = dat_ap_IMERG.isel(time=slice(-1, None))
    
    rainfall_5 = dat_5["precipitation"].mean(dim='time')
    rainfall_5 = np.transpose(np.where(rainfall_5 < 0, np.nan, rainfall_5.values))
    rainfall_1 = dat_1["precipitation"].mean(dim='time')
    rainfall_1 = np.transpose(np.where(rainfall_1 < 0, np.nan, rainfall_1.values))
    O1, O2, MWmean = onset_agro_bis(dat_ap_IMERG["precipitation"].stack(grid=["lat", "lon"]).values, dat_ap_IMERG['time'].values.shape[0], 1, 5, mwmean, 10, 5, 30)
    O1 = O1[0].reshape((len(dat_ap_IMERG["lat"]), len(dat_ap_IMERG["lon"])))
    O2 = O2[0].reshape((len(dat_ap_IMERG["lat"]), len(dat_ap_IMERG["lon"])))
    plot_graph_mod(O1, "[IMERG] Onset Occurences - Without Dryspell", [], dat_ap_IMERG, "IMERG_onset_occ_wo_dryspell", yesdate)
    plot_graph_mod(O2, "[IMERG] Onset Occurences - With Dryspell", [], dat_ap_IMERG, "IMERG_onset_occ_w_dryspell", yesdate)  
    plot_graph_mod(rainfall_5, "[IMERG] 5-day Rainfall Average", [0,1,2,3,4,5,6,7,8,9], dat_5, "IMERG_five_day_rain", yesdate)
    plot_graph_mod(rainfall_1, "[IMERG] 1-day Rainfall Average", [0,1,2,3,4,5,6,7,8,9], dat_1, "IMERG_one_day_rain", yesdate)
    
    get_onset_csv(O1, dat_ap_IMERG, output_dir)
    # Step (3) IMD Plots
    file_dir= base / "raw" / "IMD"
    file_pattern = os.path.join(file_dir, 'regrid_2025*.nc4')
    nc_files = sorted(glob.glob(file_pattern))
    
    filtered_files = []
    for file in nc_files:
        basename = os.path.basename(file)
        try:
            date_str = basename.split('_')[1].split('.')[0]  # '2025-04-01'
            date_int = int(date_str)        # '20250401'
            if date_int >= 20250401:
                filtered_files.append(file)
        except Exception as e:
            logging.error(f"Error processing file {file}: {e}")
            continue
    dat = xr.open_mfdataset(filtered_files, combine='by_coords')
    dat_ap = dat
    dat_5 = dat_ap.isel(time=slice(-5, None))
    dat_1 = dat_ap.isel(time=slice(-1, None))
    rainfall_5 = dat_5["rain"].mean(dim='time')
    rainfall_5 = np.where(rainfall_5 < 0, np.nan, rainfall_5.values)
    rainfall_1 = dat_1["rain"].mean(dim='time')
    rainfall_1 = np.where(rainfall_1 < 0, np.nan, rainfall_1.values)
    O1, O2, MWmean = onset_agro_bis(dat_ap["rain"].stack(grid=["lat", "lon"]).values, dat_ap['time'].values.shape[0], 1, 5, mwmean, 10, 5, 30)
    O1 = O1[0].reshape((len(dat_ap["lat"]), len(dat_ap["lon"])))
    O2 = O2[0].reshape((len(dat_ap["lat"]), len(dat_ap["lon"])))
    plot_graph_mod(O1, "[IMD] Onset Occurences - Without Dryspell", [], dat_ap, "IMD_onset_occ_wo_dryspell", yesdate)
    plot_graph_mod(O2, "[IMD] Onset Occurences - With Dryspell", [], dat_ap, "IMD_onset_occ_w_dryspell", yesdate)  
    plot_graph_mod(rainfall_5, "[IMD] 5-day Rainfall Average", [0,1,2,3,4,5,6,7,8,9], dat_5, "IMD_five_day_rain", yesdate)
    plot_graph_mod(rainfall_1, "[IMD] 1-day Rainfall Average", [0,1,2,3,4,5,6,7,8,9], dat_1, "IMD_one_day_rain", yesdate)
    
    # Step (4) Timeseries Plots
    
    dat_ap['time'] = dat_ap['time'] - pd.Timedelta(days=1) # This is needed because the IMD date predicts the previous 24 hours UTC but IMERG produces the 24 hours of that date (ie. IMD of 6/3 counts for 6/2)
    
    df_path = base / "data" / "grid_2x2_dissem.csv"
    df = pd.read_csv(df_path)

    df_1d = df[df["dissem33_15"] == 1]
    coords = list(df_1d[['lat', 'lon']].itertuples(index=False, name=None))
    for lat, lon in coords:
        fig, ax = plt.subplots(figsize=(6, 4))
        # dat_ap.sel(lat=lat, lon=lon, method="nearest")["precipitation"].plot(ax=ax, marker='.')
        dat_ap.sel(lat=lat, lon=lon, method="nearest")["rain"].plot(ax=ax, marker='.', label="IMD")
        dat_ap_IMERG.sel(lat=lat, lon=lon, method="nearest")["precipitation"].plot(ax=ax, marker='.', label="IMERG")
        fig.autofmt_xdate()
        ax.set_xlabel("Date")
        ax.set_title(f'{lat}N, {lon}E, Rainfall')
        ax.legend()
        fp = f'{lat}N_{lon}E_TS'
        save_path = output_dir / fp
        fig.savefig(save_path, dpi=100, bbox_inches='tight')
        plt.close()

def get_cluster():
    base = Path(__file__).resolve().parent.parent.parent
    config_file = base / ".config" / "config.json"
    with open(config_file, "r") as f:
        config = json.load(f)
    cluster = config["cluster"]
    logging.info(f"Cluster: {cluster}")
    return cluster

def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDD format",
    )
    args = parser.parse_args()
    date_f = args.date
    cluster = get_cluster()
    if cluster == "derecho":
        process_IMD_IMERG(date_f)
    else:
        process(date_f)

    logging.info("Processing completed successfully.")

if __name__ == "__main__":
    main()
