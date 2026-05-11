import xarray as xr
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import logging
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

def compute_somali_jet_index(u850_daily, v850_daily):
    """
    Computes the Somali Jet Index as the square root of the domain-mean kinetic energy
    over the region 50E–70E, 5S–20N from u850 and v850.

    Parameters:
    - u850_daily, v850_daily: xarray.DataArray with dims (time, day, lat, lon)

    Returns:
    - sji: xarray.DataArray with dims (time, day)
    """
    # Subset region: 5°S to 20°N, 50°E to 70°E
    u_sub = u850_daily.sel(lat=slice(20, -5), lon=slice(50, 70))
    v_sub = v850_daily.sel(lat=slice(20, -5), lon=slice(50, 70))

    # Compute kinetic energy: KE = 0.5 * (u^2 + v^2)
    ke = 0.5 * (u_sub**2 + v_sub**2)

    # Area weighting using cos(latitude)
    weights = np.cos(np.deg2rad(u_sub.lat))
    ke_weighted = ke.weighted(weights)

    # Mean KE over region, then SJI = sqrt(2 * mean(KE)) = sqrt(mean(u² + v²))
    mean_ke = ke_weighted.mean(dim=("lat", "lon"))
    sji = np.sqrt(2 * mean_ke)

    return sji


def plot_sji_with_valid_dates(sji, save_dir=None):
    """
    Plot Somali Jet Index vs valid date.
    Assumes sji has dimensions (time=1, day=41).
    Saves the plot with init date in filename if save_dir is provided.

    Parameters:
    - sji: xarray.DataArray with dims (time, day)
    - save_dir: optional directory to save PNG (e.g., './figures')
    """
    init_time = pd.to_datetime(sji.time.values[0])
    forecast_days = sji.day.values
    valid_dates = init_time + pd.to_timedelta(forecast_days, unit='D')

    plt.figure(figsize=(12, 5))
    plt.axhline(y=11.55, color='red', linestyle='--', label='Threshold (10 m/s)')
    plt.plot(valid_dates, sji[0], marker='o', color='black', linewidth=2)
    plt.title('Somali Jet Index', fontsize=14)
    plt.xlabel('Valid Date')
    plt.ylabel('SJI (m/s)')
    plt.ylim(2, 18)
    plt.grid(True, linestyle='--', alpha=0.6)

    # Set ticks every 4 days
    ax = plt.gca()
    tick_locs = valid_dates[::4]
    ax.set_xticks(tick_locs)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
    plt.xticks(rotation=45)

    plt.tight_layout()

    # Save if path provided
    if save_dir is not None:
        init_str = init_time.strftime('%Y%m%d')
        filename = f"{save_dir}/somali_jet_index_{init_str}.png"
        plt.savefig(filename, dpi=300, bbox_inches='tight')
        logging.info(f"Saved: {filename}")
        plt.close()
    else:
        plt.show()


def compute_webster_yang_index(u200_daily, u850_daily):
    """
    Computes the Webster & Yang Monsoon Index as the area-mean vertical shear:
    u200 - u850 over 0–20N, 40–110E.

    Parameters:
    - u200_daily, u850_daily: xarray.DataArray with dims (time, day, lat, lon)

    Returns:
    - wym_index: xarray.DataArray with dims (time, day)
    """
    # Subset both levels to the same region
    u200_sub = u200_daily.sel(lat=slice(20, 0), lon=slice(40, 110))
    u850_sub = u850_daily.sel(lat=slice(20, 0), lon=slice(40, 110))

    # Compute vertical shear
    shear = u200_sub - u850_sub

    # Area-weighted mean (weight by cos(lat))
    weights = np.cos(np.deg2rad(shear.lat))
    shear_weighted = shear.weighted(weights)
    wym_index = shear_weighted.mean(dim=('lat', 'lon'))

    return wym_index

def plot_wym_index_with_valid_dates(wym_index, save_dir=None):
    """
    Plot Webster & Yang Monsoon Index vs valid date.
    Assumes wym_index has dimensions (time=1, day=41).
    Saves the figure with init date in the filename if save_dir is given.
    """
    init_time = pd.to_datetime(wym_index.time.values[0])
    forecast_days = wym_index.day.values
    valid_dates = init_time + pd.to_timedelta(forecast_days, unit='D')

    plt.figure(figsize=(12, 5))
    plt.plot(valid_dates, wym_index[0], marker='o', color='black', linewidth=2)
    plt.title('Webster & Yang Monsoon Index', fontsize=14)
    plt.xlabel('Valid Date')
    plt.ylabel('u200 - u850 (m/s)')
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.ylim(-30, 15)  # Adjust this based on your data range

    ax = plt.gca()
    tick_locs = valid_dates[::4]
    ax.set_xticks(tick_locs)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
    plt.xticks(rotation=45)
    plt.tight_layout()

    if save_dir is not None:
        init_str = init_time.strftime('%Y%m%d')
        filename = f"{save_dir}/webster_yang_index_{init_str}.png"
        plt.savefig(filename, dpi=300, bbox_inches='tight')
        logging.info(f"Saved: {filename}")
        plt.close()
    else:
        plt.show()



def plot_sji(input_path, save_dir):
    ds = xr.open_dataset(input_path)

    def compute_daily_mean(var_name):
        var_name["step"] = var_name["step"].astype(int)
        var_name["step"] = var_name["step"] - 6
        var_name["day"] = var_name["step"] // 24
        var_name = var_name.groupby("day").mean(dim="step")
        return var_name

    u850_daily = compute_daily_mean(ds['u_850'])
    v850_daily = compute_daily_mean(ds['v_850'])
    u200_daily = compute_daily_mean(ds['u_200'])
    v200_daily = compute_daily_mean(ds['v_200'])


    sji = compute_somali_jet_index(u850_daily,v850_daily)
    plot_sji_with_valid_dates(sji ,save_dir=save_dir) ## Need to change this to the forecast directory

    wym_index = compute_webster_yang_index(u200_daily, u850_daily)
    plot_wym_index_with_valid_dates(wym_index,save_dir=save_dir)
