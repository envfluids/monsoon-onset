import logging
from pathlib import Path
import numpy as np
import pandas as pd
from netCDF4 import Dataset
from scipy.io import loadmat
from utils import find_onset, compute_quasi_onset

def process_aifs(tp_file: Path,
                 mat_file: Path,
                 allowed_cells: pd.DataFrame,
                 window: int = 5,
                 max_day: int = 40,
                 min_day: int = 1) -> pd.DataFrame:
    logging.info("Processing AIFS")

    # Load thresholds
    mat = loadmat(str(mat_file))
    mat_lat = mat['lat'].flatten()
    mat_lon = mat['lon'].flatten()
    onset_thresh = mat.get('onset_day_thres', mat.get('onset.thres'))

    # Read AIFS TP file
    if not tp_file.exists():
        logging.warning(f"AIFS TP missing: {tp_file}")
        return pd.DataFrame()
    with Dataset(tp_file) as nc:
        tp_data = nc.variables['tp'][:]       # [day, time, lat, lon]
        tvals   = nc.variables['time'][:]
        dvals   = nc.variables['day'][:]
        time_units = nc.variables['time'].units

    tp = np.transpose(tp_data, (3, 2, 1, 0))  # (nlon, nlat, ntime, nday)

    # Build forecast dates
    origin = pd.to_datetime(time_units.split(' since ')[1])
    fdates = origin + pd.to_timedelta(tvals, unit='D')

    # Filter days by min/max + window
    valid_days = np.where((dvals >= min_day) & (dvals <= max_day + window - 1))[0]

    # Identify coordinates directly from allowed_cells
    coords = []
    for _, row in allowed_cells.iterrows():
        latv = row['lat']; lonv = row['lon']
        li = np.where(mat_lat == latv)[0]
        lj = np.where(mat_lon == lonv)[0]
        if li.size and lj.size:
            coords.append((li[0], lj[0]))

    if not coords:
        logging.warning("No valid coordinates based on allowed_cells")
        return pd.DataFrame()

    # Placeholder for wind/TCW
    sji = None
    tcw = None

    # Loop over coords and forecasts
    records = []
    for (li, lj) in coords:
        thr = onset_thresh[li, lj]
        for ti, fdate in enumerate(fdates):
            series = tp[lj, li, ti, valid_days]
            for idx, day_idx in enumerate(valid_days):
                if day_idx > max_day:
                    break
                onset = find_onset(series, window, thr)
                prob = 1.0 if onset == (day_idx + 1) else 0.0
                qprob = np.mean(compute_quasi_onset(series, window, thr))
                vwind = sji[ti, idx] if sji is not None else np.nan
                vtcw  = tcw[lj, li, ti, idx] if tcw is not None else np.nan

                records.append({
                    'time': fdate,
                    'day': dvals[idx],
                    'predicted_prob': prob,
                    'predicted_quasi_prob': qprob,
                    'lat': mat_lat[li],
                    'lon': mat_lon[lj],
                    'aifs_rain_daily': series[idx],
                    'v_wind': vwind,
                    'v_tcw': vtcw
                })

    return pd.DataFrame(records)
