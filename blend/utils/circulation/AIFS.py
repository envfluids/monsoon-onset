import xarray as xr
import numpy as np
import os
import pandas as pd
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
from matplotlib.colors import ListedColormap
import matplotlib.dates as mdates
import matplotlib.patches as patches
import logging
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)


def compute_daily_mean(var_name):
    var_name["step"] = var_name["step"].astype(int)
    var_name["step"] = var_name["step"] - 6
    var_name["day"] = var_name["step"] // 24
    var_name = var_name.groupby("day").mean(dim="step")
    return var_name

def process_tp(tp):
    tp = tp*1000
    tp["step"] = tp["step"].astype(int)
    tp["step"] = tp["step"] - 6
    tp["day"] = tp["step"] // 24
    # Now set 'day' as a coordinate
    #ds_model_TS = tp.set_coords("day")
    ds_model_TS_daily = tp.groupby("day").sum(dim="step")
    #ds_model_TS_daily = tp.transpose("day", "time", "lat", "lon")
    return ds_model_TS_daily


def get_precip3_16lev_cmap():
    colors = [
        "#FFFFFF", "#C6FFFF", "#82FFFF", "#4CE6E6",
        "#00CCCC", "#00B2B2", "#00A000", "#1DB200",
        "#4CD600", "#99FF00", "#CCFF00", "#FFFF00",
        "#FFCC00", "#FF9900", "#FF0000", "#CC0000"
    ]
    return ListedColormap(colors, name="precip3_16lev")

def plot_tp_with_mslp_contours(tp_daily, mslp_daily, init_time_index, forecast_day_index, save_path=None):
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

    # Subset region and data
    tp_sub = tp_daily.isel(time=init_time_index, day=forecast_day_index).sel(
        lon=slice(50, 107), lat=slice(37, 0)
    )
    mslp_sub = mslp_daily.isel(time=init_time_index, day=forecast_day_index).sel(
        lon=slice(50, 107), lat=slice(37, 0)
    )

    # Colormap for precip
    cmap = get_precip3_16lev_cmap()

    # Plotting
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})

    # Precipitation as pcolormesh
    im = ax.pcolormesh(tp_sub.lon, tp_sub.lat, tp_sub,
                       cmap=cmap, shading='auto', vmin=0, vmax=50)

    # MSLP contours
    cs = ax.contour(mslp_sub.lon, mslp_sub.lat, mslp_sub, 
                    levels=np.arange(90000, 108000, 200),
                    colors='black', linewidths=0.8)
    ax.clabel(cs, inline=True, fontsize=8, fmt='%d')

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
    plt.suptitle('AIFS: Precipitation (shading) and MSLP (contour)', fontsize=12, y=0.83)

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


def plot_circulation_quiver(u850_daily, v850_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u850_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u850_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u = u850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(50, 107), lat=slice(37, 0))
    v = v850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(50, 107), lat=slice(37, 0))

    stride = 5
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.lon.values[::stride]
    lats = u.lat.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([50, 107, 0, 37], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Quiver and legend
    q = ax.quiver(lon2d, lat2d, u_plot.values, v_plot.values, scale=500, width=0.0015)

    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                         facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

# Add quiver key (make sure it has higher zorder so it's on top of the patch)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E',
                  coordinates='axes', color='black', zorder=2)
    #qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E', coordinates='axes', color='black')
    #qk.text.set_backgroundcolor('white')
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
    plt.suptitle('AIFS: 850 hPa Wind', fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()




def plot_circulation_quiver_200hpa(u850_daily, v850_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u850_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u850_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u = u850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(50, 107), lat=slice(37, 0))
    v = v850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(50, 107), lat=slice(37, 0))

    stride = 5
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.lon.values[::stride]
    lats = u.lat.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([50, 107, 0, 37], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Quiver and legend
    q = ax.quiver(lon2d, lat2d, u_plot.values, v_plot.values, scale=1000, width=0.0015)

    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                         facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

# Add quiver key (make sure it has higher zorder so it's on top of the patch)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=30, label='30 m/s', labelpos='E',
                  coordinates='axes', color='black', zorder=2)
    #qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E', coordinates='axes', color='black')
    #qk.text.set_backgroundcolor('white')
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
    plt.suptitle('AIFS: 200 hPa Wind', fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()




def plot_tcw_contourf(tcw_daily, init_time_index, forecast_day_index,
                      save_path=None, cmap='viridis', vmin=None, vmax=None, levels=None):
    """
    Plots Total Column Water (tcw_daily) using filled contours (contourf).

    Parameters:
    - tcw_daily: xarray.DataArray with dims (time, day, lat, lon)
    - init_time_index: index in time dimension
    - forecast_day_index: index in forecast day dimension
    - save_path: if provided, saves the figure
    - cmap: colormap name
    - vmin, vmax: colorbar range (optional)
    - levels: optional contour levels (e.g., np.linspace(0, 70, 15))
    """
    data = tcw_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(50, 107), lat=slice(37, 0))
    lons, lats = data.lon, data.lat
    lon2d, lat2d = np.meshgrid(lons, lats)

    init_time = np.datetime_as_string(tcw_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(tcw_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([50, 107, 0, 37], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Default levels if not provided
    if levels is None:
        levels = np.linspace(vmin if vmin is not None else np.nanmin(data),
                             vmax if vmax is not None else np.nanmax(data), 15)

    cf = ax.contourf(lon2d, lat2d, data, levels=levels, cmap=cmap, extend='max')

    # Ticks with degree labels
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
    plt.suptitle('AIFS: Total Column Water (TCW)', fontsize=12, y=0.83)

    # Colorbar
    cbar = fig.colorbar(cf, ax=ax, orientation='vertical', fraction=0.03, pad=0.02)
    cbar.set_label('kg/m²')

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()


def plot_wndspd850_aifs(u850_daily, v850_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u850_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u850_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u = u850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(30, 130), lat=slice(40, -40))
    v = v850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(30, 130), lat=slice(40, -40))

    # Compute wind speed
    wind_speed = np.sqrt(u**2 + v**2)

    # Grid for quivers (downsampled)
    stride = 12
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.lon.values[::stride]
    lats = u.lat.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)

    # Full grid for pcolormesh
    lon_full, lat_full = np.meshgrid(u.lon, u.lat)

    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([30, 130, -40, 40], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Background wind speed
    speed_plot = ax.pcolormesh(lon_full, lat_full, wind_speed, transform=ccrs.PlateCarree(),
                               cmap='YlGnBu', shading='auto',vmin=0, vmax=30)
    cbar = plt.colorbar(speed_plot, ax=ax, orientation='vertical', pad=0.02, shrink=0.7)
    cbar.set_label('Wind Speed (m/s)', fontsize=10)

    # Quiver
    q = ax.quiver(lon2d, lat2d, u_plot.values, v_plot.values, scale=500, width=0.0015)

    # Quiver key background patch
    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                             facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

    # Quiver key
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E',
                      coordinates='axes', color='black', zorder=2)

    # Axis ticks
    xticks = np.arange(30, 130, 20)
    yticks = np.arange(-40, 50, 20)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}" for y in yticks], fontsize=10)

    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')

    # Titles
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle('AIFS: 850 hPa Wind', fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()

def plot_wndspd200_aifs(u850_daily, v850_daily, init_time_index, forecast_day_index, save_path=None):
    init_time = np.datetime_as_string(u850_daily.time[init_time_index].values, unit='D')
    valid_time = np.datetime_as_string(
        u850_daily.time[init_time_index].values + np.timedelta64(forecast_day_index, 'D'), unit='D')

    # Subset
    u = u850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(30, 130), lat=slice(40, -40))
    v = v850_daily.isel(time=init_time_index, day=forecast_day_index).sel(lon=slice(30, 130), lat=slice(40, -40))
    # Compute wind speed
    wind_speed = np.sqrt(u**2 + v**2)
    
    stride = 12
    u_plot = u[::stride, ::stride]
    v_plot = v[::stride, ::stride]
    lons = u.lon.values[::stride]
    lats = u.lat.values[::stride]
    lon2d, lat2d = np.meshgrid(lons, lats)
  # Full grid for pcolormesh
    lon_full, lat_full = np.meshgrid(u.lon, u.lat)
    # Plot
    fig, ax = plt.subplots(figsize=(10, 8), subplot_kw={'projection': ccrs.PlateCarree()})
    ax.set_extent([30, 130, -40, 40], crs=ccrs.PlateCarree())
    ax.coastlines(resolution='10m', linewidth=1)

    # Background wind speed
    speed_plot = ax.pcolormesh(lon_full, lat_full, wind_speed, transform=ccrs.PlateCarree(),
                               cmap='YlGnBu', shading='auto',vmin=0, vmax=60)
    cbar = plt.colorbar(speed_plot, ax=ax, orientation='vertical', pad=0.02, shrink=0.7)
    cbar.set_label('Wind Speed (m/s)', fontsize=10)
    
    # Quiver and legend
    q = ax.quiver(lon2d, lat2d, u_plot.values, v_plot.values, scale=1000, width=0.0015)

    rect = patches.Rectangle((0.85, 0.92), 0.22, 0.07, transform=ax.transAxes,
                         facecolor='white', edgecolor='none', zorder=1)
    ax.add_patch(rect)

# Add quiver key (make sure it has higher zorder so it's on top of the patch)
    qk = ax.quiverkey(q, X=0.9, Y=0.95, U=30, label='30 m/s', labelpos='E',
                  coordinates='axes', color='black', zorder=2)
    #qk = ax.quiverkey(q, X=0.9, Y=0.95, U=10, label='10 m/s', labelpos='E', coordinates='axes', color='black')
    #qk.text.set_backgroundcolor('white')
    # Axis ticks
    xticks = np.arange(30, 130, 20)
    yticks = np.arange(-40, 50, 20)
    ax.set_xticks(xticks, crs=ccrs.PlateCarree())
    ax.set_yticks(yticks, crs=ccrs.PlateCarree())
    ax.set_xticklabels([f"{x}°E" for x in xticks], fontsize=10)
    ax.set_yticklabels([f"{y}°N" for y in yticks], fontsize=10)

    ax.gridlines(draw_labels=False, linewidth=0.5, color='gray', linestyle='--')

    # Titles
    ax.set_title(f'Init: {init_time}', loc='left', fontsize=11)
    ax.set_title(f'Valid: {valid_time}', loc='right', fontsize=11)
    plt.suptitle('AIFS: 200 hPa Wind', fontsize=12, y=0.87)

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        logging.info(f"Saved: {save_path}")
    else:
        plt.show()


def plot_aifs(input_path, save_dir):
    # Data directory [Make dyanmic for different day forecast]
    # data_dir = '/glade/derecho/scratch/marchakitus/monsoon/full_field_ref' # Directory where the data is stored
    # aifs_fname = 'AIFS_20250428T12.nc' # AIFS file name
    # aifs_file_path = os.path.join(data_dir, aifs_fname)
    ds = xr.open_dataset(input_path)

    ### Now plot Precip and MSLP Maps for valid day 7, 14, 21 and 28

    lead_days = [7, 14, 21, 28]  # Forecast day indices for day 7, 14, 21, 28

    u850_daily = compute_daily_mean(ds['u_850'])
    v850_daily = compute_daily_mean(ds['v_850'])
    u200_daily = compute_daily_mean(ds['u_200'])
    v200_daily = compute_daily_mean(ds['v_200'])
    tp_daily = process_tp(ds['tp'])
    mslp_daily = compute_daily_mean(ds['msl'])
    tcw_daily = compute_daily_mean(ds['tcw'])

    for day in lead_days:
        tp_save_name = save_dir / f"AIFS_precip_mslp_lead_day_{day}.png" ### Path needs to be changed for automation
        plot_tp_with_mslp_contours(
            tp_daily=tp_daily,
            mslp_daily=mslp_daily,
            init_time_index=0,  # Adjust if using different init time
            forecast_day_index=day,
            save_path=tp_save_name
        )
        tcw_save_name = save_dir / f"AIFS_tcw_lead_day_{day}.png" ### Path needs to be changed for automation
        plot_tcw_contourf(tcw_daily, init_time_index=0, forecast_day_index=day, 
                    save_path = tcw_save_name, cmap='Blues', vmin=0, vmax=70)
        
        wind850_filename = save_dir / f'AIFS_u850_v850_wind_day_{day}.png' ### Path needs to be changed for automation
        plot_circulation_quiver(u850_daily, v850_daily, init_time_index=0,
                            forecast_day_index=day, save_path=wind850_filename)
        windspd850_filename = save_dir / f'AIFS_windspd850_day_{day}.png' ### Path needs to be changed for automation
        plot_wndspd850_aifs(u850_daily, v850_daily, init_time_index=0,
                            forecast_day_index=day, save_path=windspd850_filename)
        wind200_filename = save_dir/ f'AIFS_u200_v200_wind_day_{day}.png' ### Path needs to be changed for automation
        plot_circulation_quiver_200hpa(u200_daily, v200_daily, init_time_index=0,
                            forecast_day_index=day, save_path=wind200_filename)
        windspd200_filename = save_dir / f'AIFS_windspd200_day_{day}.png' ### Path needs to be changed for automation
        plot_wndspd200_aifs(u200_daily, v200_daily, init_time_index=0,
                            forecast_day_index=day, save_path=windspd200_filename)
