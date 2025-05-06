import numpy as np
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.cm import get_cmap
import matplotlib.cm as cm
from matplotlib.colors import Normalize
from matplotlib.lines import Line2D
from datetime import timedelta
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
import warnings
import os
from pathlib import Path
import geopandas as gpd
from shapely.geometry import box

from MoronRobertson_F import onset_agro_bis

warnings.filterwarnings('ignore')

def forecast_onset_cal_NeuralGCM_GFS(ds_model, number, MWMean):
    ds_model_TS_daily = ds_model.isel(number=number)
    ds_model_TS_daily_np = ds_model_TS_daily['tp'].T.values
    ds_model_daily_np_Onset = ds_model_TS_daily_np.reshape(ds_model_TS_daily_np.shape[0]*ds_model_TS_daily_np.shape[1],1)  # number of initializations * number of days
    lseason = ds_model_TS_daily_np.shape[1]
    defdry = 1
    sw = 5
    wet = MWMean
    sd = 10
    dry = 5
    window = 10
    o1_model, o2_model, MWMean_model = onset_agro_bis(ds_model_daily_np_Onset, lseason, defdry, sw, wet, sd, dry, window)
    #print(o1_model)
    model_Onset = pd.DataFrame(o1_model, columns = ['Onset_Days'])
    model_Onset['Date_Init'] = ds_model['time'].dt.date
    model_Onset['Onset_Date'] = model_Onset.apply(lambda x: x.Date_Init + pd.Timedelta(days = (x.Onset_Days+1)) if pd.notnull(x['Onset_Days']) and pd.notnull(x['Date_Init'])
    else np.nan, axis=1)
    model_Onset['Onset_Date'] = pd.to_datetime(model_Onset['Onset_Date'])
    model_Onset['Date_Init'] = pd.to_datetime(model_Onset['Date_Init'])
    filtered_df = model_Onset
    return filtered_df

def get_week_bin(onset_date, init_date):
    if pd.isna(onset_date):
        return None
    delta_days = (onset_date - init_date).days
    if delta_days < 0: return None # Or handle as needed
    if 0 <= delta_days <= 7: return 'Week 1'
    elif 8 <= delta_days <= 14: return 'Week 2'
    elif 15 <= delta_days <= 21: return 'Week 3'
    elif 22 <= delta_days <= 28: return 'Week 4'
    elif delta_days > 28: return 'Week Later'
    else: return None

def NGCM_Prob_Calc_first(df_ngcm, lat, lon, df):
    df_ngcm_grid = df_ngcm.sel(lat = lat, lon = lon)
    time = pd.to_datetime(df_ngcm_grid.time.values)
    #df_MWMean = xr.open_dataset("/glade/u/home/mgupta/MWMean01_1901_2023_2deg.nc")
    #MWMean = df_MWMean.sel(lat = lat, lon = lon)['MWMean'].item()
    MWMean = df.loc[(df.lat == lat) & (df.lon == lon), "onset_thresh"].iloc[0]

    final_Onset = {}
    for number in range(30):
        filtered_df = forecast_onset_cal_NeuralGCM_GFS(df_ngcm_grid, number, MWMean)
        final_Onset[number] = filtered_df  # Store filtered_df under the (fy, number) key pair
    all_ensemble_data = pd.DataFrame()
    for i in range(30):  # Ensemble members 0 to 29
        ensemble_member_data = final_Onset[i]
        df_ensemble = ensemble_member_data[['Date_Init', 'Onset_Date']].copy()
        df_ensemble.set_index('Date_Init', inplace=True)
        df_ensemble.rename(columns={'Onset_Date': f'Onset_Date_{i+1}'}, inplace=True)
        # Merge this ensemble's data with the existing DataFrame
        if all_ensemble_data.empty:
            all_ensemble_data = df_ensemble
        else:
            all_ensemble_data = all_ensemble_data.join(df_ensemble)
    # --- Make sure 'Date_Init' is the index and is datetime ---
    if 'Date_Init' in all_ensemble_data.columns:
        all_ensemble_data['Date_Init'] = pd.to_datetime(all_ensemble_data['Date_Init'])
        all_ensemble_data = all_ensemble_data.set_index('Date_Init')
    else:
        # If Date_Init is already the index, just convert it
        all_ensemble_data.index = pd.to_datetime(all_ensemble_data.index)
    onset_cols = [col for col in all_ensemble_data.columns if col.startswith('Onset_Date_')]
    all_ensemble_data[onset_cols] = all_ensemble_data[onset_cols].apply(pd.to_datetime, errors='coerce')
    # -- 3. Apply Binning and Count (Same as before) --
    stacked_data = all_ensemble_data[onset_cols].stack().reset_index()
    stacked_data.columns = ['Date_Init', 'Ensemble_Member', 'Onset_Date']
    stacked_data['Bin'] = stacked_data.apply(
        lambda row: get_week_bin(row['Onset_Date'], row['Date_Init']),
        axis=1
    )
    bin_counts = stacked_data.groupby('Date_Init')['Bin'].value_counts().unstack(fill_value=0)
    bin_order = ['Week 1', 'Week 2', 'Week 3', 'Week 4', 'Week Later']
    bin_counts = bin_counts.reindex(columns=bin_order, fill_value=0)
    # -- 4. Calculate Probabilities --
    total_ensembles = len(onset_cols) # Use the actual number of ensemble columns
    bin_prob = bin_counts / total_ensembles
    #bin_prob.to_csv(f"NGCM_Probabilites_{lat}_{lon}.csv")

    week_to_end_day = {'Week 1': 7, 'Week 2': 14, 'Week 3': 21, 'Week 4': 28, 'Week Later': 35}

    # Reshape probabilities to long format
    df_melted = bin_prob.reset_index().melt(
        id_vars=["Date_Init"],
        value_vars=bin_order,
        var_name="forecast_week",
        value_name="probability"
    )
    # Calculate the absolute end date for each forecast week bin
    df_melted["end_day_relative"] = df_melted["forecast_week"].map(week_to_end_day)
    df_melted["forecast_end_date"] = df_melted["Date_Init"] + pd.to_timedelta(df_melted["end_day_relative"], unit="D")
    # Calculate the start date (end date - 7 days)
    df_melted["forecast_start_date"] = df_melted["forecast_end_date"] - pd.Timedelta(days=7)
    # Create row level for y-axis positioning
    unique_times = sorted(df_melted["Date_Init"].unique())
    time_to_row = {t: i for i, t in enumerate(unique_times)}
    df_melted["row"] = df_melted["Date_Init"].map(time_to_row)
    return df_melted

def plot_precip(date):
    base = Path(__file__).resolve().parent.parent.parent
    onsets_2024_path = base / "blend" / "data" / "support" / "onsets_2024.csv"
    df_onset_dates = pd.read_csv(onsets_2024_path)
    df_meta = df_onset_dates[['lat', 'lon', 'onset_thresh']].copy()
    df_meta = df_meta.drop_duplicates(subset=['lat', 'lon'])

    
    aifs_tp_file = base / "AIFS" / "output" / "tp" / f"tp_{date}.nc"
    ngcm_precip_file = base / "NeuralGCM" / "output" / "tp" / f"tp_{date}.nc"

    df_ngcm = xr.open_dataset(ngcm_precip_file)
    df_AIFS = xr.open_dataset(aifs_tp_file)

    # Final Ensemble Plots
    # dateF = "20250427"

    path_out = base / "blend" / "output" / date / "precip_plots" 

    if not os.path.exists(path_out):
        os.makedirs(path_out)

    # 0) Metadata
    df_metadata = df_meta.copy()

    ts64 = df_ngcm['time'].isel(time=0).item()
    date_init = pd.to_datetime(ts64).normalize()
    init_date = pd.Timestamp(date_init.date())

    for _, meta in df_metadata.iterrows():
        lat = meta.lat
        lon = meta.lon
        onset_thresh = meta.onset_thresh

        # A) build ensemble time‐series
        da = df_ngcm.sel(lat=lat, lon=lon).rename({"day":"TIME"})
        roll = da.rolling(TIME=5, min_periods=1).sum().shift(TIME=-4)
        roll = roll.isel(TIME=slice(0, -4))
        roll["RAINFALL_Ini"] = da["tp"]
        roll = roll.rename({"tp":"rolling5"})

        df_ts = roll.to_dataframe().reset_index()
        df_ts["datetime"] = df_ts["time"] + pd.to_timedelta(df_ts["TIME"], unit="D")
        df_daily = df_ts.pivot(index="datetime", columns="number", values="RAINFALL_Ini")
        df_roll  = df_ts.pivot(index="datetime", columns="number", values="rolling5")


        da_AIFS = df_AIFS.sel(lat=lat, lon=lon).rename({"day":"TIME"})
        roll_AIFS = da_AIFS.rolling(TIME=5, min_periods=1).sum().shift(TIME=-4)
        roll_AIFS = roll_AIFS.isel(TIME=slice(0, -4))
        roll_AIFS["RAINFALL_Ini"] = da_AIFS["tp"]
        roll_AIFS = roll_AIFS.rename({"tp":"rolling5"})
        
        df_ts_AIFS = roll_AIFS.to_dataframe().reset_index()
        df_ts_AIFS["datetime"] = df_ts_AIFS["time"] + pd.to_timedelta(df_ts_AIFS["TIME"], unit="D")

        df_daily_AIFS = df_ts_AIFS[['datetime', 'RAINFALL_Ini']]
        df_roll_AIFS = df_ts_AIFS[['datetime', 'rolling5']]
        df_daily_AIFS = df_daily_AIFS.set_index("datetime")
        df_roll_AIFS = df_roll_AIFS.set_index("datetime")


        # B) build probability bars
        df_prob = NGCM_Prob_Calc_first(df_ngcm, lat, lon, df_metadata)
        group   = df_prob.query("Date_Init==@init_date").sort_values("forecast_start_date")

        # C) plotting
        fig, (ax1, ax2, ax3) = plt.subplots(
            3,1, sharex=True, figsize=(10,6),
            gridspec_kw={"height_ratios":[1,1,1], "hspace":0.1}
        )

        # Panel 1: daily precip
        dates1 = df_daily.index
        pos1   = mdates.date2num(dates1.to_pydatetime())
        data1  = [df_daily.loc[d].values for d in dates1]
        ax1.boxplot(
            data1, positions=pos1, widths=1.6, patch_artist=True, showfliers=False,
            boxprops=dict(facecolor="lightgray", edgecolor="gray", alpha=0.6),
            whiskerprops=dict(color="gray"),
            capprops=dict(color="gray"),
            medianprops=dict(color="black")
        )
        ax1.plot(dates1, df_daily.mean(axis=1), color="blue", lw=1.5, label="NGCM Ensemble Mean")
        
        #data_AIFS  = [df_daily_AIFS.loc[d].values for d in dates1]
        ax1.plot(df_daily_AIFS.index, df_daily_AIFS['RAINFALL_Ini'], color="crimson", lw=1.5, label="AIFS")

        y1 = onset_thresh/5
        ax1.axhline(y1, color="black", linestyle="--", lw=1.5,
                    label=f"Avg Daily Precip Threshold: {y1:.2f} mm")
        ax1.set_ylabel("Daily Precip (mm)", fontsize=9)
        ax1.legend(loc="upper left", fontsize=7)
        ax1.grid(True, linestyle="--", alpha=0.4)
        ax1.tick_params(labelsize=8, labelbottom=False)

        # Panel 2: rolling sum
        dates2 = df_roll.index
        pos2   = mdates.date2num(dates2.to_pydatetime())
        data2  = [df_roll.loc[d].values for d in dates2]
        ax2.boxplot(
            data2, positions=pos2, widths=1.6, patch_artist=True, showfliers=False,
            boxprops=dict(facecolor="lightgray", edgecolor="gray", alpha=0.6),
            whiskerprops=dict(color="gray"),
            capprops=dict(color="gray"),
            medianprops=dict(color="black")
        )
        ax2.plot(dates2, df_roll.mean(axis=1), color="blue", lw=1.5, label="NGCM Ensemble Mean")
        ax2.plot(df_roll_AIFS.index, df_roll_AIFS['rolling5'], color="crimson", lw=1.5, label="AIFS")

        y2 = onset_thresh
        ax2.axhline(y2, color="black", linestyle="--", lw=1.5,
                    label=f"Avg 5-Day Rolling Threshold: {y2:.2f} mm")
        ax2.set_ylabel("5-Day Rolling Sum (mm)", fontsize=9)
        ax2.legend(loc="upper left", fontsize=7)
        ax2.grid(True, linestyle="--", alpha=0.4)
        ax2.tick_params(labelsize=8, labelbottom=False)

        # Panel 3: probability bars
        for _, dr in group.iterrows():
            ax3.barh(
                y=0,
                left=dr["forecast_start_date"],
                width=(dr["forecast_end_date"] - dr["forecast_start_date"]).days,
                height=dr["probability"],
                facecolor="lightgray",
                edgecolor="black",
                linewidth=1.5,
                align="edge"
            )
        # 2-week onset chance as red line
        max_sum, bs, be = -1, None, None
        for i in range(len(group)-1):
            s = group.probability.iloc[i] + group.probability.iloc[i+1]
            if s > max_sum:
                max_sum, bs, be = s, group.forecast_start_date.iloc[i], group.forecast_end_date.iloc[i+1]
        if bs:
            ax3.hlines(y=-0.05, xmin=bs, xmax=be, color="red", linewidth=2, label="2-Week Onset Chance")

        ax3.set_ylim(-0.1, 1.0)
        ax3.set_yticks(np.linspace(0,1,6))
        ax3.set_ylabel("Prob. of Onset", fontsize=9)
        ax3.yaxis.tick_left()
        ax3.tick_params(axis='y', labelsize=8, rotation=0)
        ax3.grid(True, linestyle="--", alpha=0.4)

        # common x-axis formatting
        ax3.xaxis.set_major_locator(mdates.DayLocator(interval=2))
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d-%b"))
        ax3.set_xlabel("Forecast Date", fontsize=9)
        ax3.tick_params(axis='x', labelsize=8, rotation=90)

        ax3.legend(loc="upper right", fontsize=7)

        fig.suptitle(f"Initialization {init_date.date()} — Lat={lat}, Lon={lon}", y=0.96, fontsize=11)
        # folder_out = f"/glade/u/home/mgupta/ProbForecastMetric/Ensemble plots/Daily_5Day_Prob_Plots/{dateF}/"
        # if not os.path.exists(folder_out):
        #     os.makedirs(folder_out)
        outfn = f"ensembles_and_prob_lat{lat}_lon{lon}.png"
        out_ens_prob = path_out / outfn
        fig.savefig(out_ens_prob, dpi=300, bbox_inches="tight")
        plt.close(fig)
        print(f"Saved {outfn}")

    # 0) Metadata
    df_metadata = df_meta.copy()

    ts64 = df_ngcm['time'].isel(time=0).item()
    date_init = pd.to_datetime(ts64).normalize()
    init_date = pd.Timestamp(date_init.date())

    for _, meta in df_metadata.iterrows():
        lat = meta.lat
        lon = meta.lon
        onset_thresh = meta.onset_thresh

        # A) build ensemble time‐series
        da = df_ngcm.sel(lat=lat, lon=lon).rename({"day":"TIME"})
        roll = da.rolling(TIME=5, min_periods=1).sum().shift(TIME=-4)
        roll = roll.isel(TIME=slice(0, -4))
        roll["RAINFALL_Ini"] = da["tp"]
        roll = roll.rename({"tp":"rolling5"})

        df_ts = roll.to_dataframe().reset_index()
        df_ts["datetime"] = df_ts["time"] + pd.to_timedelta(df_ts["TIME"], unit="D")
        df_daily = df_ts.pivot(index="datetime", columns="number", values="RAINFALL_Ini")
        df_roll  = df_ts.pivot(index="datetime", columns="number", values="rolling5")


        da_AIFS = df_AIFS.sel(lat=lat, lon=lon).rename({"day":"TIME"})
        roll_AIFS = da_AIFS.rolling(TIME=5, min_periods=1).sum().shift(TIME=-4)
        roll_AIFS = roll_AIFS.isel(TIME=slice(0, -4))
        roll_AIFS["RAINFALL_Ini"] = da_AIFS["tp"]
        roll_AIFS = roll_AIFS.rename({"tp":"rolling5"})
        
        df_ts_AIFS = roll_AIFS.to_dataframe().reset_index()
        df_ts_AIFS["datetime"] = df_ts_AIFS["time"] + pd.to_timedelta(df_ts_AIFS["TIME"], unit="D")

        df_daily_AIFS = df_ts_AIFS[['datetime', 'RAINFALL_Ini']]
        df_roll_AIFS = df_ts_AIFS[['datetime', 'rolling5']]
        df_daily_AIFS = df_daily_AIFS.set_index("datetime")
        df_roll_AIFS = df_roll_AIFS.set_index("datetime")


        # B) build probability bars
        df_prob = NGCM_Prob_Calc_first(df_ngcm, lat, lon, df_metadata)
        group   = df_prob.query("Date_Init==@init_date").sort_values("forecast_start_date")

        # C) plotting
        fig, (ax2, ax3) = plt.subplots(
            2,1, sharex=True, figsize=(10,4),
            gridspec_kw={"height_ratios":[1,1], "hspace":0.1}
        )

        # Panel 2: rolling sum
        dates2 = df_roll.index
        pos2   = mdates.date2num(dates2.to_pydatetime())
        data2  = [df_roll.loc[d].values for d in dates2]
        ax2.boxplot(
            data2, positions=pos2, widths=1.6, patch_artist=True, showfliers=False,
            boxprops=dict(facecolor="lightgray", edgecolor="gray", alpha=0.6),
            whiskerprops=dict(color="gray"),
            capprops=dict(color="gray"),
            medianprops=dict(color="black")
        )
        ax2.plot(dates2, df_roll.mean(axis=1), color="blue", lw=1.5, label="NGCM Ensemble Mean")
        ax2.plot(df_roll_AIFS.index, df_roll_AIFS['rolling5'], color="crimson", lw=1.5, label="AIFS")

        y2 = onset_thresh
        ax2.axhline(y2, color="black", linestyle="--", lw=1.5,
                    label=f"Avg 5-Day Rolling Threshold: {y2:.2f} mm")
        ax2.set_ylabel("5-Day Rolling Sum (mm)", fontsize=9)
        ax2.legend(loc="upper left", fontsize=7)
        ax2.grid(True, linestyle="--", alpha=0.4)
        ax2.tick_params(labelsize=8, labelbottom=False)

        # Panel 3: probability bars
        for _, dr in group.iterrows():
            ax3.barh(
                y=0,
                left=dr["forecast_start_date"],
                width=(dr["forecast_end_date"] - dr["forecast_start_date"]).days,
                height=dr["probability"],
                facecolor="lightgray",
                edgecolor="black",
                linewidth=1.5,
                align="edge"
            )
        # 2-week onset chance as red line
        max_sum, bs, be = -1, None, None
        for i in range(len(group)-1):
            s = group.probability.iloc[i] + group.probability.iloc[i+1]
            if s > max_sum:
                max_sum, bs, be = s, group.forecast_start_date.iloc[i], group.forecast_end_date.iloc[i+1]
        if bs:
            ax3.hlines(y=-0.05, xmin=bs, xmax=be, color="red", linewidth=2, label="2-Week Onset Chance")

        ax3.set_ylim(-0.1, 1.0)
        ax3.set_yticks(np.linspace(0,1,6))
        ax3.set_ylabel("Prob. of Onset", fontsize=9)
        ax3.yaxis.tick_left()
        ax3.tick_params(axis='y', labelsize=8, rotation=0)
        ax3.grid(True, linestyle="--", alpha=0.4)

        # common x-axis formatting
        ax3.xaxis.set_major_locator(mdates.DayLocator(interval=2))
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d-%b"))
        ax3.set_xlabel("Forecast Date", fontsize=9)
        ax3.tick_params(axis='x', labelsize=8, rotation=90)

        ax3.legend(loc="upper right", fontsize=7)

        fig.suptitle(f"Initialization {init_date.date()} — Lat={lat}, Lon={lon}", y=0.96, fontsize=11)
        # folder_out = f"/glade/u/home/mgupta/ProbForecastMetric/Ensemble plots/5Day_Prob_Plots/{dateF}/"
        # if not os.path.exists(folder_out):
        #     os.makedirs(folder_out)
        outfn = f"5_Day_lat{lat}_lon{lon}_ensembles_and_prob_.png"
        out_5_day = path_out / outfn
        fig.savefig(out_5_day, dpi=300, bbox_inches="tight")
        plt.close(fig)
        print(f"Saved {outfn}")





    ts64 = df_ngcm['time'].isel(time=0).item()
    date_init = pd.to_datetime(ts64).normalize()
    init_date = pd.Timestamp(date_init.date())


    # --- Shared binning function (from Code 1) ---
    def get_week_bin(onset_date, init_date):
        if pd.isna(onset_date):
            return None
        d = (onset_date - init_date).days
        if d < 0:
            return None
        if   0 <= d <= 7:
            return 'Week 1'
        elif  8 <= d <= 14:
            return 'Week 2'
        elif 15 <= d <= 21:
            return 'Week 3'
        elif 22 <= d <= 28:
            return 'Week 4'
        else:
            return 'Week Later'

    BIN_ORDER = ['Week 1','Week 2','Week 3','Week 4','Week Later']

    # --- PARAMETERS ---
    INIT_DATE   = init_date
    india_shapefile = base / "blend" / "data" / "india_shapefile" / "India_Country_Boundary.shp"
    WEEKS       = BIN_ORDER[:-1]       # only plot Weeks 1–4
    CMAP        = "YlGnBu"
    OUTPUT_FILE = "monsoon_onset_probs_week1to4.png"

    # --- LOAD INDIA BOUNDARY ---
    india = gpd.read_file(india_shapefile).to_crs("EPSG:4326")
    minx, miny, maxx, maxy = india.total_bounds
    xticks = np.arange(np.floor(minx), np.ceil(maxx)+1, 2)
    yticks = np.arange(np.floor(miny), np.ceil(maxy)+1, 2)

    # --- METADATA ---
    df_metadata = df_meta.copy()

    # --- PROB CALC FUNCTION using shared get_week_bin & BIN_ORDER ---
    def NGCM_Prob_Calc(ds_ngcm, lat, lon, df_meta):
        grid = ds_ngcm.sel(lat=lat, lon=lon)
        MWMean    = df_meta.loc[(df_meta.lat==lat)&(df_meta.lon==lon),'onset_thresh'].iloc[0]
        
        all_ens = {
        n: forecast_onset_cal_NeuralGCM_GFS(grid, n, MWMean)
        for n in range(30)
        }

        df_all = pd.DataFrame()
        for n, df_e in all_ens.items():
            tmp = df_e[['Date_Init','Onset_Date']].copy()
            tmp['Onset_Date'] = pd.to_datetime(tmp['Onset_Date'])
            tmp = tmp.set_index('Date_Init').rename(columns={'Onset_Date':f'Onset_Date_{n+1}'})
            df_all = df_all.join(tmp, how='outer') if not df_all.empty else tmp

        df_all.index = pd.to_datetime(df_all.index)
        onset_cols = [c for c in df_all.columns if c.startswith('Onset_Date_')]
        df_long = df_all[onset_cols].stack().reset_index(name='Onset_Date')
        df_long['Bin'] = df_long.apply(lambda r: get_week_bin(r['Onset_Date'], r['Date_Init']), axis=1)

        counts = (df_long.groupby('Date_Init')['Bin']
                    .value_counts()
                    .unstack(fill_value=0)
                    .reindex(columns=BIN_ORDER, fill_value=0))
        probs = counts.div(30)

        week_days = {'Week 1':7,'Week 2':14,'Week 3':21,'Week 4':28,'Week Later':35}
        dfm = (probs.reset_index()
                .melt(id_vars='Date_Init', var_name='forecast_week', value_name='probability'))
        dfm['end_date']   = dfm['Date_Init'] + pd.to_timedelta(dfm['forecast_week'].map(week_days), 'D')
        dfm['start_date'] = dfm['end_date'] - pd.Timedelta(days=7)
        dfm['lat'], dfm['lon'] = lat, lon
        return dfm

    # --- COLLECT & PLOT Weeks 1–4 ---
    records = []
    for _, m in df_metadata.iterrows():
        dfp = NGCM_Prob_Calc(df_ngcm, m.lat, m.lon, df_metadata)
        dfp = dfp.query("Date_Init == @INIT_DATE")
        if not dfp.empty:
            records.append(dfp)

    df_all = pd.concat(records, ignore_index=True)

    fig, axes = plt.subplots(1, 4, figsize=(12, 5), sharey=True)
    for ax, wk in zip(axes, WEEKS):
        sub = df_all.query("forecast_week == @wk")
        agg = sub.groupby(['lat','lon'], as_index=False)['probability'].mean()
        agg['geometry'] = agg.apply(lambda r: box(r.lon-1, r.lat-1, r.lon+1, r.lat+1), axis=1)
        gdf = gpd.GeoDataFrame(agg, geometry='geometry', crs='EPSG:4326')

        india.plot(ax=ax, facecolor='none', edgecolor='black')
        gdf.plot(ax=ax, column='probability', cmap=CMAP, vmin=0, vmax=1,
                edgecolor='white', linewidth=0.5)

        for _, r in agg.iterrows():
            ax.text(r.lon, r.lat, f"{r.probability*100:.0f}", ha='center', va='center', fontsize=8)

        ax.set_title(wk)
        ax.set_xticks(xticks); ax.set_yticks(yticks)
        ax.set_xlim(minx, maxx); ax.set_ylim(miny, maxy)
        ax.set_xlabel('Longitude')
        if ax is axes[0]:
            ax.set_ylabel('Latitude')
        ax.grid(True, linestyle='--', alpha=0.5)
        ax.tick_params(axis='both', labelsize=8)

    # horizontal colorbar at bottom
    sm = plt.cm.ScalarMappable(cmap=CMAP, norm=plt.Normalize(vmin=0, vmax=1))
    sm._A = []
    cbar = fig.colorbar(sm, ax=axes.tolist(), orientation='horizontal',
                        fraction=0.03, pad = -0.2)
    cbar.set_label('Probability', fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    plt.suptitle('Monsoon Onset Probabilities by Week (2°×2° boxes)', y=0.98, fontsize=12)
    plt.tight_layout(rect=[0,0,1,0.95])
    # folder_out = f"/glade/u/home/mgupta/ProbForecastMetric/Ensemble plots/Spatial_4Week_Plots/{dateF}/"
    # if not os.path.exists(folder_out):
    #     os.makedirs(folder_out)
    week1to4_out = path_out / OUTPUT_FILE
    fig.savefig(week1to4_out, dpi=300, bbox_inches='tight')
    #plt.close(fig)
    print(f"Saved {OUTPUT_FILE}")

def main():
    # date = "20250506T12"
    # plot_precip(date)
    #NGCM_Prob_Calc(df_ngcm, lat, lon, df_meta)
    #plot_precip()
    pass

if __name__ == "__main__":
    main()