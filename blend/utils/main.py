import logging
from pathlib import Path
import pandas as pd
from utils import compute_roll_sum
from ngcm import process_ngcm
from aifs import process_aifs_offset
# from aifs import process_aifs
import numpy as np
import argparse
import os
from blend import blend
from maps import make_maps
from plot_precip import plot_precip
from circulation.circulation_main import plot_circulation
from messages import generate_messages
from datetime import datetime, timedelta

def parse_date(date_str):
    """
    Parses a date string in the format 'YYYYMMDDTHH' and returns a datetime object.

    Args:
        date_str (str): The date string to parse, e.g., '20250428T14'.

    Returns:
        datetime: A datetime object representing the parsed date and time.
    """
    try:
        return datetime.strptime(date_str, "%Y%m%dT%H")
    except ValueError as e:
        raise ValueError(
            f"Invalid date format: {date_str}. Expected format 'YYYYMMDDTHH'."
        ) from e

def get_data(date, base):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
    )
    out_path = base / "blend" / "output" / date
    out_file = out_path / f"all_data.csv"
    # copies of each of these files are in the py folder I sent Adam
    support_dir = base / "blend" / "data" / "support"
    thresholds_file = support_dir / "thresholds_df.csv"
    clusters_file = support_dir / "onset_clusters.csv"
    mat_file = support_dir / "onset_five_day_thres_2deg.mat"
    clim_file = support_dir / "large" / "ensemble_outputs_clim_2025.csv"
    allowed_cells_file = support_dir / "allowed_cells.csv"

    AIFS_date = parse_date(date) - timedelta(hours=12)
    AIFS_date = AIFS_date.strftime("%Y%m%dT%H")
    aifs_tp_file = base / "AIFS" / "output" / "tp" / f"tp_{AIFS_date}.nc"
    logging.info(f"Using AIFS file: {aifs_tp_file} for base date: {date}")

    # aifs_tp_file = base / "AIFS" / "output" / "tp" / f"tp_{date}.nc"
    ngcm_precip_file = base / "NeuralGCM" / "output" / "tp" / f"tp_{date}.nc"
    logging.info(f"Using NGCM file: {ngcm_precip_file} for base date: {date}")

    if aifs_tp_file.exists() and ngcm_precip_file.exists():
        logging.info(f"Blending AIFS and NGCM data for {date}")
    else:
        if not aifs_tp_file.exists():
            logging.error(f"Missing AIFS data for {date}")
        if not ngcm_precip_file.exists():
            logging.error(f"Missing NGCM data for {date}")
        return None, None
    os.makedirs(out_path, exist_ok=True)
    allowed_cells = pd.read_csv(allowed_cells_file)

    # Process forecasts
    print("WITH OFFSET")
    ngcm_df = process_ngcm(ngcm_precip_file, mat_file, allowed_cells)
    print("NGCM DF")
    print(ngcm_df)
    # aifs_df = process_aifs(aifs_tp_file, mat_file, allowed_cells)
    aifs_df = process_aifs_offset(aifs_tp_file, mat_file, allowed_cells)
    print("AIFS DF")
    print(aifs_df)
    ngcm_df["time"] = pd.to_datetime(ngcm_df["time"]).dt.normalize()
    aifs_df["time"] = pd.to_datetime(aifs_df["time"]).dt.normalize()

    # Load additional data
    clim = pd.read_csv(clim_file)
    clim["time"] = pd.to_datetime(clim["time"]).dt.normalize()
    thresholds = pd.read_csv(thresholds_file)
    clusters = pd.read_csv(clusters_file)

    # Merge CV data
    ngcm_sel = ngcm_df.rename(columns={"v_wind": "ngcm_wind", "v_tcw": "ngcm_tcw"})
    merged = ngcm_sel.merge(
        aifs_df.rename(columns={"v_wind": "aifs_wind", "v_tcw": "aifs_tcw"}),
        on=["time", "day", "lat", "lon"],
        how="inner",
    )
    print("MERGED")
    print(merged)

    # read in climatology data
    clim_wide = clim.pivot_table(
        index=["time", "day", "lat", "lon"], columns="model", values="predicted_prob"
    ).reset_index()
    all_data = merged.merge(clim_wide, on=["time", "day", "lat", "lon"], how="inner")
    all_data["year"] = all_data["time"].dt.year
    all_data["date"] = all_data["time"] + pd.to_timedelta(all_data["day"], unit="D")

    # -------------------------------------------------------------------------
    # COMPUTE DAILY ROLLING TOTALS (5‑day & 10‑day)
    # -------------------------------------------------------------------------
    all_data = all_data.sort_values(["lat", "lon", "time", "day"])

    for var in ["ngcm_rain_daily", "aifs_rain_daily"]:
        if var in all_data.columns:
            # group the filled series and apply compute_roll_sum to each chunk
            grp = all_data.groupby(["lat", "lon", "time"])[var]
            all_data[f"{var}_5day_total"] = grp.transform(
                lambda x: compute_roll_sum(x.fillna(0).to_numpy(), 5)
            )
            all_data[f"{var}_10day_total"] = grp.transform(
                lambda x: compute_roll_sum(x.fillna(0).to_numpy(), 10)
            )

    # all_data.to_csv(out, index=False)

    # -------------------------------------------------------------------------
    # BIN INTO WEEKS
    # -------------------------------------------------------------------------
    bins = [0, 7, 14, 21, 28, np.inf]
    labels = ["week1", "week2", "week3", "week4", "later"]
    all_data["interval"] = pd.cut(all_data["day"], bins=bins, labels=labels, right=True)

    model_cols = [
        c for c in clim_wide.columns if c not in ["time", "day", "lat", "lon"]
    ]

    # -------------------------------------------------------------------------
    # AGGREGATE BY (time,lat,lon,interval)
    #   - daily sums stay sums
    #   - 5‑day rolling totals → max
    #   - 10‑day rolling totals → min
    #   - climatology probs stay sums (we’ll rename later)
    # -------------------------------------------------------------------------
    agg_dict = {
        "ngcm_rain_daily": "sum",
        "aifs_rain_daily": "sum",
        "ngcm_rain_daily_5day_total": "max",
        "aifs_rain_daily_5day_total": "max",
        "ngcm_rain_daily_10day_total": "min",
        "aifs_rain_daily_10day_total": "min",
        **{m: "sum" for m in model_cols},
    }

    agg = (
        all_data.groupby(["time", "lat", "lon", "interval"]).agg(agg_dict).reset_index()
    )

    # -------------------------------------------------------------------------
    # PIVOT TO WIDE
    # -------------------------------------------------------------------------
    wide = agg.pivot_table(
        index=["time", "lat", "lon"], columns="interval", values=list(agg_dict.keys())
    ).reset_index()

    # flatten MultiIndex like ('ngcm_rain_daily_5day_total','week1') → 'ngcm_rain_daily_5day_total_week1'
    wide.columns = [
        "_".join(filter(None, col)).strip("_") for col in wide.columns.values
    ]

    # -------------------------------------------------------------------------
    # RENAME CLIMATOLOGY & ROLLING‑SUM COLUMNS
    # -------------------------------------------------------------------------
    # 1) climatology: e.g. 'clim_mr_week1' → 'prob_clim_mr_week1'
    for m in model_cols:
        for wk in labels:
            old = f"{m}_{wk}"
            new = f"prob_{m}_{wk}"
            if old in wide.columns:
                wide = wide.rename(columns={old: new})

    # 2) rolling‑sum stats:
    #    'ngcm_rain_daily_5day_total_week1' → 'max_ngcm_5day_week1', etc.
    for src in ["ngcm", "aifs"]:
        for wk in labels:
            o5 = f"{src}_rain_daily_5day_total_{wk}"
            n5 = f"max_{src}_5day_{wk}"
            if o5 in wide.columns:
                wide = wide.rename(columns={o5: n5})

            o10 = f"{src}_rain_daily_10day_total_{wk}"
            n10 = f"min_{src}_10day_{wk}"
            if o10 in wide.columns:
                wide = wide.rename(columns={o10: n10})

    final = wide.merge(clusters, on=["lat", "lon"], how="left").merge(
        thresholds, on=["lat", "lon"], how="left"
    )

    for src in ['ngcm', 'aifs']:
        for wk in labels:
            max_col  = f'max_{src}_5day_{wk}'
            diff_col = f'diff_{src}_{wk}'
            final[diff_col] = final[max_col] - final['onset_threshold']
    # -------------------------------------------------------------------------
    # TRANSFORM climatology probabilities per week
    #  - npc: logit only
    #  - mr:  logit + scale, drop "_all" suffix
    #  - quasi: logit + scale
    # -------------------------------------------------------------------------
    from scipy.special import logit

    weeks = ["week1", "week2", "week3", "week4", "later"]
    # 1) NPC: logit only
    for prefix in ["prob_clim_npc_all", "prob_clim_npc_post"]:
        for wk in weeks:
            col = f"{prefix}_{wk}"
            if col in final.columns:
                final[col] = logit(final[col].clip(lower=0.0001, upper=0.99))

    # 2) MR: logit + scale, then rename "_all" → no suffix
    for pref in ["prob_clim_mr_all", "prob_clim_mr_post"]:
        for wk in weeks:
            old = f"{pref}_{wk}"
            if old in final.columns:
                x = logit(final[old].clip(0.0001, 0.99))
                # build new name: drop "_all" but keep "_post" if present
                if pref.endswith("_all"):
                    new = f"prob_clim_mr_{wk}"
                else:
                    new = f"prob_clim_mr_post_{wk}"
                final[new] = x
                # remove the old column if renamed
                if new != old:
                    final.drop(columns=[old], inplace=True)
    # 3) QUASI: logit + scale (keep names)
    for prefix in ["prob_clim_quasi", "prob_clim_quasi_post"]:
        for wk in weeks:
            col = f"{prefix}_{wk}"
            if col in final.columns:
                x = logit(final[col].clip(0.0001, 0.99))
                final[col] = x

    final.to_csv(out_file, index=False)
    logging.info(
        f"CV data with rolling‑sum stats and transformed climatology written to {out_file}"
    )

    return out_path, final


def copy_to_latest(origin_path, dest_path):
    """Copy files from origin_path to dest_path and remove existing files in dest_path."""

    command = f"rm -r {dest_path}/*"
    try:
        os.system(command)
        logging.info(f"Removed existing files in: {dest_path}")
    except Exception as e:
        logging.error(f"Error removing symbolic link: {e}")
        raise e

    command = f"cp -r {origin_path} {dest_path}"
    try:
        os.system(command)
        logging.info(f"Copied files from {origin_path} to {dest_path}")
    except Exception as e:
        logging.error(f"Error copying files: {e}")
        raise e
    finally:
        logging.info("Copy operation completed.")


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDDTHH format",
    )
    args = parser.parse_args()
    date = args.date
    base = Path(__file__).resolve().parent.parent.parent
    sync_path = base / "sync" / "latest"
    out_path, final = get_data(date, base)
    if final is None:
        logging.error("No data to process")
        return
    else:
        logging.info(f"Initializing blend")
        summary = blend(final, date)
        logging.info(f"Blending completed for {date}")
        logging.info(f"Generating messages")
        generate_messages(base, out_path, summary)
        logging.info(f"Messages generated for {date}")
        logging.info("Creating maps")
        make_maps(summary, date)
        logging.info(f"Maps created for {date}")
        logging.info("Plotting precipitation")
        plot_precip(date)
        logging.info(f"Precipitation plots created for {date}")
        logging.info("Plotting circulation")
        plot_circulation(base, date)
        logging.info(f"Circulation plots created for {date}")
        copy_to_latest(out_path, sync_path)
        logging.info(f"FINAL: COMPUTE PIPELINE COMPLETE FOR {date}")

if __name__ == "__main__":
    main()
