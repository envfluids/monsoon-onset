"""
Generic forecast processor.

process_forecast() converts a raw model NetCDF file into a long-format
DataFrame of subdistrict-level daily precipitation and onset probabilities.
It works for any model described by a ModelConfig; the only model-specific
choices are declared in that config object (see utils/models/config.py).

Adding a new model
------------------
1. Create utils/models/<modelname>.py with a ModelConfig instance.
2. Point weights_file at that model's regridding weights CSV.
3. Import the config in main.py and add it to ACTIVE_MODELS.
4. No changes to this file are required.
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from netCDF4 import Dataset

from utils import find_onset, compute_quasi_onset
from models.config import ModelConfig

log = logging.getLogger(__name__)


def process_forecast(
    tp_file: Path,
    config: ModelConfig,
    thresholds: pd.DataFrame,
    window: int = 5,
    max_day: int = 40,
    min_day: int = 1,
    pipeline_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Process one model's precipitation NetCDF and return subdistrict daily rain.

    Parameters
    ----------
    tp_file : Path
        NetCDF file containing the precipitation forecast.
        Deterministic dim order: [day, time, lat, lon]
        Ensemble dim order:      [day, ens, time, lat, lon]
    config : ModelConfig
        Model-specific settings (label, grid, ensemble flag, etc.).
    thresholds : pd.DataFrame
        Columns: id (int), onset_thresh (float).
        Onset-detection threshold for each subdistrict (mm cumulative over
        `window` days).
    window : int
        Rolling-sum window for onset detection (days).
    max_day : int
        Maximum forecast lead day to retain in output.
    min_day : int
        Minimum forecast lead day to retain in output.

    Returns
    -------
    pd.DataFrame
        Columns:
          id                   – subdistrict integer ID
          time                 – forecast initialisation date (datetime64)
          day                  – forecast lead day (int)
          {label}_rain_daily   – regridded daily precipitation (mm)
          predicted_prob       – P(onset on this day) given forecast
          predicted_quasi_prob – quasi-onset probability on this day
    """
    if not tp_file.exists():
        log.warning("Missing forecast file: %s", tp_file)
        return pd.DataFrame()

    # ── 1. Load regridding weights ─────────────────────────────────────────────
    weights_df = pd.read_csv(config.weights_file)
    # Normalise column names (allow both spellings seen in practice)
    weights_df.columns = [c.strip().lower() for c in weights_df.columns]
    # Rename target_id / source_id if the file uses slightly different names
    col_map = {}
    for col in weights_df.columns:
        if "target" in col:
            col_map[col] = "target_id"
        elif "source" in col:
            col_map[col] = "source_id"
    if col_map:
        weights_df = weights_df.rename(columns=col_map)

    # Parse source lat/lon from strings like "10.00_77.25"
    def _parse_sid(sid: str):
        parts = sid.split("_")
        return round(float(parts[0]), 2), round(float(parts[1]), 2)

    unique_sids = weights_df["source_id"].unique()
    sid_latlon = {sid: _parse_sid(sid) for sid in unique_sids}

    # ── 2. Load NetCDF ─────────────────────────────────────────────────────────
    with Dataset(tp_file) as nc:
        var = nc.variables[config.precip_var]
        tp_raw = var[:]  # masked array
        var_dims = var.dimensions
        tvals = nc.variables["time"][:]
        dvals = nc.variables["day"][:].astype(float)
        time_units = nc.variables["time"].units
        nc_lats = np.round(nc.variables["lat"][:].astype(float), 2)
        nc_lons = np.round(nc.variables["lon"][:].astype(float), 2)

    tp_raw = np.ma.filled(tp_raw, np.nan)

    # Normalise axis order regardless of how the NetCDF stored it.
    # Target: deterministic -> (day, time, lat, lon)
    #         ensemble      -> (day, number, time, lat, lon)
    target_order = ["day", "number" if config.is_ensemble else None, "time", "lat", "lon"]
    target_order = [d for d in target_order if d is not None]
    if list(var_dims) != target_order:
        try:
            perm = [var_dims.index(d) for d in target_order]
        except ValueError as exc:
            raise ValueError(
                f"{config.label}: NetCDF variable '{config.precip_var}' has dims "
                f"{var_dims}; expected a permutation of {target_order}."
            ) from exc
        tp_raw = np.transpose(tp_raw, perm)

    # Apply AIFS-style day-1 skip and day-number shift
    if config.skip_first_day:
        tp_raw = tp_raw[1:]
        dvals = dvals[1:] - 1.0

    # Determine the forecast-date label for each init time.
    # Operationally each forecast file contains a single init time, and we label
    # outputs with the pipeline date that the user passed in (not the NetCDF's
    # internal time coord, which may be the model init time on a different
    # calendar day — e.g. AIFS is initialised at 12Z of the previous day).
    if pipeline_date is not None and len(tvals) == 1:
        fdates = pd.DatetimeIndex([pd.Timestamp(pipeline_date).normalize()])
    else:
        if pipeline_date is not None:
            log.warning(
                "%s: file %s has %d time entries; ignoring pipeline_date and "
                "decoding from NetCDF time variable.",
                config.label, tp_file, len(tvals),
            )
        origin = pd.to_datetime(time_units.split(" since ")[1])
        fdates = origin + pd.to_timedelta(tvals, unit="D")
        fdates = pd.DatetimeIndex(fdates).normalize()

    # ── 3. Map source cells to NetCDF lat/lon indices ─────────────────────────
    # Build lookup: (rounded_lat, rounded_lon) → (lat_idx, lon_idx)
    lat_lookup = {v: i for i, v in enumerate(nc_lats)}
    lon_lookup = {v: i for i, v in enumerate(nc_lons)}

    sid_to_idx: dict[str, tuple[int, int]] = {}
    for sid, (slat, slon) in sid_latlon.items():
        li = lat_lookup.get(slat)
        lj = lon_lookup.get(slon)
        if li is None or lj is None:
            log.warning(
                "Source cell %s (lat=%.2f, lon=%.2f) not found in %s NetCDF grid; "
                "skipping this cell.",
                sid, slat, slon, config.label,
            )
        else:
            sid_to_idx[sid] = (li, lj)

    # Keep only weights whose source cell was matched
    weights_df = weights_df[weights_df["source_id"].isin(sid_to_idx)].copy()
    if weights_df.empty:
        log.error("No source cells matched for %s – returning empty DataFrame.", config.label)
        return pd.DataFrame()

    # ── 4. Build sparse weight matrix (n_targets × n_sources) ─────────────────
    target_ids = sorted(weights_df["target_id"].unique())
    valid_sids = sorted(sid_to_idx.keys())
    n_targets = len(target_ids)
    n_sources = len(valid_sids)
    tid_row = {tid: i for i, tid in enumerate(target_ids)}
    sid_col = {sid: i for i, sid in enumerate(valid_sids)}

    W = np.zeros((n_targets, n_sources), dtype=np.float32)
    for _, row in weights_df.iterrows():
        r = tid_row.get(row["target_id"])
        c = sid_col.get(row["source_id"])
        if r is not None and c is not None:
            W[r, c] = float(row["weight"])

    # NumPy index arrays for extracting source cells from the NetCDF array
    li_arr = np.array([sid_to_idx[s][0] for s in valid_sids], dtype=int)
    lj_arr = np.array([sid_to_idx[s][1] for s in valid_sids], dtype=int)

    # ── 5. Select valid day indices ────────────────────────────────────────────
    # Keep up to max_day + window - 1 so rolling sums have enough look-ahead.
    valid_mask = (dvals >= min_day) & (dvals <= max_day + window - 1)
    valid_idxs = np.where(valid_mask)[0]
    proc_days = dvals[valid_idxs].astype(int)

    # ── 6. Extract source-cell precip and reweight to subdistricts ────────────
    # After slicing valid days, shape is:
    #   deterministic: [n_valid_days, n_times, nlat, nlon]
    #   ensemble:      [n_valid_days, n_ens,   n_times, nlat, nlon]
    raw_sel = tp_raw[valid_idxs]

    if config.is_ensemble:
        # Average over ensemble members first so all downstream logic is the same
        # raw_sel: [days, ens, time, lat, lon] → mean over axis 1
        raw_sel = np.nanmean(raw_sel, axis=1)   # [days, time, lat, lon]

    # Extract source cells: [days, time, n_sources]
    src_precip = raw_sel[:, :, li_arr, lj_arr]

    # Conservative regridding via matrix multiply:
    #   [days*time, n_sources] @ W.T → [days*time, n_targets]
    n_days, n_times, _ = src_precip.shape
    flat = src_precip.reshape(n_days * n_times, n_sources).astype(np.float64)
    sub_flat = flat @ W.T.astype(np.float64)          # [days*time, n_targets]
    sub_precip = sub_flat.reshape(n_days, n_times, n_targets)
    # sub_precip[day_idx, time_idx, target_idx]

    # ── 7. Per-subdistrict onset detection and output records ─────────────────
    thr_dict = thresholds.set_index("id")["onset_thresh"].to_dict()
    rain_col = f"{config.label}_rain_daily"
    records = []

    for ti, fdate in enumerate(fdates):
        for tgt_i, tid in enumerate(target_ids):
            thr = thr_dict.get(tid)
            if thr is None or np.isnan(thr):
                continue

            series = sub_precip[:, ti, tgt_i]   # daily precip [n_valid_days]

            onset_day = find_onset(series, window, thr)
            quasi_arr = compute_quasi_onset(series, window, thr)

            for di, d in enumerate(proc_days):
                if d > max_day:
                    break
                prob = (
                    1.0
                    if (not np.isnan(onset_day) and int(onset_day) == int(d))
                    else 0.0
                )
                qprob = float(quasi_arr[di]) if di < len(quasi_arr) else 0.0

                records.append(
                    {
                        "id": int(tid),
                        "time": fdate,
                        "day": int(d),
                        rain_col: float(series[di]),
                        "predicted_prob": prob,
                        "predicted_quasi_prob": qprob,
                    }
                )

    df = pd.DataFrame(records)
    log.info(
        "%s: %d records produced (%d subdistricts × %d times × %d days)",
        config.label,
        len(df),
        n_targets,
        len(fdates),
        len(proc_days[proc_days <= max_day]),
    )
    return df
