import xarray as xr
import numpy as np
import os
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
from matplotlib.colors import ListedColormap
import matplotlib.patches as patches
import logging
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

def preprocess(ds):
    time_first_value = (ds['time'].values[0] - np.timedelta64(6, 'h'))
    ds = ds.rename({"time":"step"})
    ds['step'] = np.arange(6, 6*len(ds.step) + 1, 6)
    ds = ds.expand_dims('time') # Ensure step is a dimension
    ds['time'] = [time_first_value]
    return ds

def calculate_tp_daily(ds_model_TS):
    ds_model_TS = ds_model_TS*1000
    ds_model_TS = ds_model_TS.diff(dim="step", label="upper")
    #ds_model_TS["precipitation_cumulative_mean"] = ds_model_TS["precipitation_cumulative_mean"] * 1000
    ds_model_TS["step"] = ds_model_TS["step"].astype(int)
    ds_model_TS["step"] = ds_model_TS["step"] - 12
    ds_model_TS["day"] = ds_model_TS["step"] // 24
    # Now set 'day' as a coordinate
    #ds_model_TS = ds_model_TS.set_coords("day")
    ds_model_TS_daily = ds_model_TS.groupby("day").sum(dim="step")
    #ds_model_TS_daily = ds_model_TS_daily.transpose("day", "time", "latitude", "longitude","ensemble")
    return ds_model_TS_daily


def ngcm_var_daily(var_name):
    var_name["step"] = var_name["step"].astype(int)
    var_name["step"] = var_name["step"] - 6
    var_name["day"] = var_name["step"] // 24
    #var_name = var_name.set_coords("day")
    var_name_daily = var_name.groupby("day").mean(dim="step")
    return var_name_daily

def get_precip3_16lev_cmap():
    colors = [
        "#FFFFFF", "#C6FFFF", "#82FFFF", "#4CE6E6",
        "#00CCCC", "#00B2B2", "#00A000", "#1DB200",
        "#4CD600", "#99FF00", "#CCFF00", "#FFFF00",
        "#FFCC00", "#FF9900", "#FF0000", "#CC0000"
    ]
    return ListedColormap(colors, name="precip3_16lev")

def plot_tp(tp_daily, init_time_index, forecast_day_index, save_path=None):
    """
    Plots daily precipitation forecast with MSLP contours for a specific forecast day.

    Parameters:
    - tp_daily: xarray.DataArray with dimensions (time, day, lat, lon)
    - mslp_daily: xarray.DataArray (same dimensions as tp_daily)
    - init_time_index: Index of the initialization time in .time
    - forecast_day_index: Index of the forecast day (0-based)
    """

    # Time strings
    init_time = np.datetime_as_string(tp_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        tp_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'),
        unit='D'
    )
    tp_sub = tp_daily.mean(dim="ensemble")
    # Subset region and data
    tp_sub = tp_sub.isel(time=init_time_index, day=forecast_day_index, surface = 0).sel(
        longitude=slice(50, 107), latitude=slice(0, 37)
    )
  
    # Colormap for precip
    cmap = get_precip3_16lev_cmap()

    # Plotting
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})

    # Precipitation as pcolormesh
    im = ax.pcolormesh(tp_sub.longitude, tp_sub.latitude, tp_sub.T,
                       cmap=cmap, shading='auto', vmin=0, vmax=50)

    # ax.clabel(cs, inline=True, fontsize=8, fmt='%d')

    # Map features
    ax.coastlines(resolution='10m', linewidth=1)
    ax.set_extent([50, 107, 0, 37], crs=ccrs.PlateCarree())
    xticks = np.arange(50, 108, 5)
    yticks = np.arange(0, 38, 5)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}°E" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}°N" for y in yticks], fontsize=10)

    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')
    # Annotations
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle('NGCM: Precipitation (ensemble mean)', fontsize=12, y=0.83)

    # Colorbar
    cbar = fig.colorbar(im, ax=ax, orientation='vertical', fraction=0.03, pad=0.02)
    cbar.set_label('Precipitation (mm)')
    cbar.set_ticks(np.linspace(0, 50, 9))

    plt.tight_layout(rect=[0, 0, 1, 0.94])  # Make space for suptitle
    # Save if path provided
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
    else:
        plt.show()

def plot_circulation_quiver_850(u_daily, v_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u_ensmean = u_daily.mean(dim="ensemble")
    v_ensmean = v_daily.mean(dim="ensemble")

    u = u_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(longitude=slice(50, 107), latitude=slice(0, 37), level = 850)
    v = v_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(longitude=slice(50, 107), latitude=slice(0, 37), level = 850)

    stride = 1
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.longitude.values[::stride]
    lats = u.latitude.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([50, 107, 0, 37], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Quiver and legend
    q = ax.quiver(lon2d, lat2d, u_plot.T.values, v_plot.T.values, scale=500, width=0.0015)
    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                         facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

    # Add quiver key (make sure it has higher zorder so it's on top of the patch)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E',
                  coordinates='axes', color='black', zorder=2)

    # Axis ticks
    xticks = np.arange(50, 108, 5)
    yticks = np.arange(0, 38, 5)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}°E" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}°N" for y in yticks], fontsize=10)

    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')

    # Titles
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle("NGCM: 850 hPa Wind (ensemble mean)", fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()

def plot_circulation_quiver_200(u_daily, v_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u_ensmean = u_daily.mean(dim="ensemble")
    v_ensmean = v_daily.mean(dim="ensemble")

    u = u_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(longitude=slice(50, 107), latitude=slice(0, 37), level = 200)
    v = v_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(longitude=slice(50, 107), latitude=slice(0, 37), level = 200)

    stride = 1
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.longitude.values[::stride]
    lats = u.latitude.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([50, 107, 0, 37], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Quiver and legend
    q = ax.quiver(lon2d, lat2d, u_plot.T.values, v_plot.T.values, scale=800, width=0.0015)
    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                         facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

    # Add quiver key (make sure it has higher zorder so it's on top of the patch)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=30, label='30 m/s', labelpos='E',
                  coordinates='axes', color='black', zorder=2)

    # Axis ticks
    xticks = np.arange(50, 108, 5)
    yticks = np.arange(0, 38, 5)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}°E" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}°N" for y in yticks], fontsize=10)

    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')

    # Titles
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle("NGCM: 200 hPa Wind (ensemble mean)", fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()


def plot_wndspd_quiver_850(u_daily, v_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset and average across ensemble
    u_ensmean = u_daily.mean(dim="ensemble")
    v_ensmean = v_daily.mean(dim="ensemble")

    u = u_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(
        longitude=slice(30, 130), latitude=slice(-40, 40), level=850)
    v = v_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(
        longitude=slice(30, 130), latitude=slice(-40, 40), level=850)

    # Calculate wind speed magnitude
    wind_speed = np.sqrt(u**2 + v**2)

    # Coordinates and stride for quiver
    stride = 1
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.longitude.values
    lats = u.latitude.values
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Plot setup
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([30, 130, -40, 40], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Plot wind speed background
    wind_plot = ax.pcolormesh(lon2d, lat2d, wind_speed.T.values, cmap='YlGnBu', shading='auto', vmin=0, vmax=30)
    cbar = plt.colorbar(wind_plot, ax=ax, orientation='vertical', pad=0.02, shrink=0.7)
    cbar.set_label('Wind Speed (m/s)')

    # Quiver plot
    q = ax.quiver(lons[::stride], lats[::stride], u_plot.T.values, v_plot.T.values,
                  scale=500, width=0.0015, color='black')

    # Quiver key background and label
    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                             facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E',
                      coordinates='axes', color='black', zorder=2)

    # Axis ticks
    xticks = np.arange(30, 131, 20)
    yticks = np.arange(-40, 41, 20)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}" for y in yticks], fontsize=10)
    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')

    # Titles
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle("NGCM: 850 hPa Wind (ensemble mean)", fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()


def plot_wndspd_quiver_200(u_daily, v_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u_ensmean = u_daily.mean(dim="ensemble")
    v_ensmean = v_daily.mean(dim="ensemble")

    u = u_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(longitude=slice(30, 130), latitude=slice(-40, 40), level = 200)
    v = v_ensmean.isel(time=init_time_index, day=forecast_day_index).sel(longitude=slice(30, 130), latitude=slice(-40, 40), level = 200)
    # Calculate wind speed magnitude
    wind_speed = np.sqrt(u**2 + v**2)
    stride = 1
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.longitude.values[::stride]
    lats = u.latitude.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([30, 130, -40, 40], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

     # Plot wind speed background
    wind_plot = ax.pcolormesh(lon2d, lat2d, wind_speed.T.values, cmap='YlGnBu', shading='auto', vmin=0, vmax=60)
    cbar = plt.colorbar(wind_plot, ax=ax, orientation='vertical', pad=0.02, shrink=0.7)
    cbar.set_label('Wind Speed (m/s)')
    
    # Quiver and legend
    q = ax.quiver(lon2d, lat2d, u_plot.T.values, v_plot.T.values, scale=800, width=0.0015)
    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                         facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

    # Add quiver key (make sure it has higher zorder so it's on top of the patch)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=30, label='30 m/s', labelpos='E',
                  coordinates='axes', color='black', zorder=2)

    # Axis ticks
    xticks = np.arange(30, 131, 20)
    yticks = np.arange(-40, 41, 20)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}°E" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}°N" for y in yticks], fontsize=10)

    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')

    # Titles
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle("NGCM: 200 hPa Wind (ensemble mean)", fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()

def plot_neuralgcm(input_path, save_dir):
    file_pattern = os.path.join(input_path,"*.zarr")

    NGCM_data = xr.open_mfdataset(file_pattern, engine='zarr', preprocess=preprocess)

    tp_cum = NGCM_data["precipitation_cumulative_mean"]
    u = NGCM_data["u_component_of_wind"]
    v = NGCM_data["v_component_of_wind"]

    lead_days = [7, 14, 21, 28]  # Forecast day indices for day 7, 14, 21, 28
    tp_daily = calculate_tp_daily(tp_cum)
    u_daily = ngcm_var_daily(u)
    v_daily = ngcm_var_daily(v)
    for day in lead_days:
        tp_save_name = save_dir /  f"ngcm_preciplead_day_{day}.png" ### Path needs to be changed for automation
        plot_tp(
            tp_daily=tp_daily,
            init_time_index=0,  # Adjust if using different init time
            forecast_day_index=day,
            save_path=tp_save_name
        )
        wind850_filename = save_dir / f'ngcm_u850_v850_wind_day_{day}.png' ### Path needs to be changed for automation
        plot_circulation_quiver_850(u_daily, v_daily, init_time_index=0,
                            forecast_day_index=day, save_path=wind850_filename)
        wind200_filename = save_dir / f'ngcm_u200_v200_wind_day_{day}.png' ### Path needs to be changed for automation
        plot_circulation_quiver_200(u_daily, v_daily, init_time_index=0,
                            forecast_day_index=day, save_path=wind200_filename)
        
        wndspd850_filename = save_dir / f'ngcm_wndspd850_wind_day_{day}.png' ### Path needs to be changed for automation
        plot_wndspd_quiver_850(u_daily, v_daily, init_time_index=0,
                            forecast_day_index=day, save_path=wndspd850_filename)
        wndspd200_filename = save_dir / f'ngcm_wndspd200_wind_day_{day}.png' ### Path needs to be changed for automation
        plot_wndspd_quiver_200(u_daily, v_daily, init_time_index=0,
                            forecast_day_index=day, save_path=wndspd200_filename)
