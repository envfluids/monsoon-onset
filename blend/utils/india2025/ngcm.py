import logging
from pathlib import Path
import numpy as np
import pandas as pd
from netCDF4 import Dataset
from scipy.io import loadmat
from multiprocessing import Pool
from functools import partial
from utils import find_onset, compute_quasi_onset
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

def _ngcm_worker(
    coord,
    mat_lat: np.ndarray,
    mat_lon: np.ndarray,
    onset_thresh: np.ndarray,
    pc: np.ndarray,
    fdates: np.ndarray,
    valid_days: np.ndarray,
    dayv: np.ndarray,
    window: int,
    max_day: int,
    wa: np.ndarray,
    tw: np.ndarray,
) -> pd.DataFrame:
    li, lj = coord
    latv = mat_lat[li]
    lonv = mat_lon[lj]
    thr = onset_thresh[li, lj]
    recs = []

    for ti, fdate in enumerate(fdates):
        raw = np.transpose(pc, (1, 2, 0, 3, 4))  # [days, ens, lat, lon]
        raw = raw[valid_days, ti, :, li, lj]  # [days, ens]
        arr = raw.T  # [ens, days]
        ens_mean = np.nanmean(arr, axis=0)
        ens_sd = np.nanstd(arr, axis=0)

        for di, d in enumerate(dayv):
            if d > max_day:
                break
            onset_idx = [
                find_onset(arr[m, :], window, thr) for m in range(arr.shape[0])
            ]
            prob = np.mean([(o == d) for o in onset_idx if not np.isnan(o)])
            qmat = np.vstack(
                [
                    compute_quasi_onset(arr[m, :], window, thr)
                    for m in range(arr.shape[0])
                ]
            )
            qprob = np.mean(qmat[:, di])

            vw = vw_sd = vt = vt_sd = None
            if wa is not None:
                vw = np.nanmean(wa[:, ti, di])
                vw_sd = np.nanstd(wa[:, ti, di])
            if tw is not None:
                vt = np.nanmean(tw[lj, li, :, ti, di])
                vt_sd = np.nanstd(tw[lj, li, :, ti, di])

            recs.append(
                {
                    "time": fdate,
                    "day": d,
                    "onset_thresh": thr,
                    "predicted_prob": prob,
                    "predicted_quasi_prob": qprob,
                    "ngcm_rain_daily": ens_mean[di],
                    "ngcm_rain_daily_sd": ens_sd[di],
                    "v_wind": vw,
                    "v_wind_sd": vw_sd,
                    "v_tcw": vt,
                    "v_tcw_sd": vt_sd,
                    "lat": latv,
                    "lon": lonv,
                }
            )
    return pd.DataFrame(recs)


def process_ngcm(
    precip_file: Path,
    mat_file: Path,
    allowed_cells: pd.DataFrame,
    window: int = 5,
    max_day: int = 40,
    min_day: int = 1,
    cutoff_month_day: str = "04-01",
) -> pd.DataFrame:
    logging.info("Processing NGCM without IMD masking")

    # Load MAT thresholds
    mat = loadmat(str(mat_file))
    mat_lat = mat["lat"].flatten()
    mat_lon = mat["lon"].flatten()
    onset_thresh = mat.get("onset_day_thres", mat.get("onset.thres"))

    # Read NGCM precipitation
    if not precip_file.exists():
        logging.warning(f"NGCM precip missing: {precip_file}")
        return pd.DataFrame()
    with Dataset(precip_file) as nc:
        pc = nc.variables["tp"][:]
        tvals2 = nc.variables["time"][:]
        day_vals = nc.variables["day"][:]
        tu = nc.variables["time"].units

    origin2 = pd.to_datetime(tu.split(" since ")[1])
    fdates = origin2 + pd.to_timedelta(tvals2, unit="D")

    year = fdates[0].year
    start_date = pd.to_datetime(f"{year}-{cutoff_month_day}")
    valid_f = np.where(fdates >= start_date)[0]

    if valid_f.size == 0:
        logging.warning(f"No NGCM forecast >= {start_date.date()}")
        return pd.DataFrame()

    pc = pc[:, :, valid_f, :, :]

    fdates = fdates[valid_f]

    valid_days = np.where((day_vals >= min_day) & (day_vals <= max_day + window - 1))[0]
    dayv = day_vals[valid_days]

    # Identify coordinates directly from allowed_cells
    coords = []
    for _, row in allowed_cells.iterrows():
        latv = row["lat"]
        lonv = row["lon"]
        li = np.where(mat_lat == latv)[0]
        lj = np.where(mat_lon == lonv)[0]
        if li.size and lj.size:
            coords.append((li[0], lj[0]))

    if not coords:
        logging.warning("No valid coordinates based on allowed_cells")
        return pd.DataFrame()

    # Read SJI (wind) - placeholder
    wa = None
    # placeholder for tcw
    tw = None

    # Prepare worker
    worker = partial(
        _ngcm_worker,
        mat_lat=mat_lat,
        mat_lon=mat_lon,
        onset_thresh=onset_thresh,
        pc=pc,
        fdates=fdates,
        valid_days=valid_days,
        dayv=dayv,
        window=window,
        max_day=max_day,
        wa=wa,
        tw=tw,
    )

    with Pool() as pool:
        dfs = pool.map(worker, coords)

    return pd.concat([df for df in dfs if not df.empty], ignore_index=True)
