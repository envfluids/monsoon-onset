"""
main.py – Monsoon onset blend pipeline (2026 edition).

Entry point
-----------
    python main.py --20260511T00

What it does
------------
1. Checks AIFS and NGCM forecast files exist.
2. Processes each active forecast model.
3. Merges model outputs with climatological probabilities.
4. Computes weekly rolling-sum features and blends them into onset
   probabilities with blend.py.
5. Generates maps with maps_subdistrict.py.
"""

import argparse
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from maps_subdistrict import make_maps
from models import AIFS_CONFIG, NGCM_CONFIG  # CHANGE 1
from process_forecast import process_forecast
from utils import compute_roll_sum

from blend import blend

# ── Active models ─────────────────────────────────────────────────────────────
ACTIVE_MODELS = [
    AIFS_CONFIG,
    NGCM_CONFIG,  # CHANGE 2
]

MODEL_CONFIGS = {
    "AIFS_single_v1p1": AIFS_CONFIG,
    "AIFS_single_v2": AIFS_CONFIG,
    "NeuralGCM": NGCM_CONFIG,
}

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


def parse_date(date_str: str) -> datetime:
    """Parse 'YYYYMMDDTHH' → datetime."""
    try:
        return datetime.strptime(date_str, "%Y%m%dT%H")
    except ValueError as exc:
        raise ValueError(
            f"Invalid date format '{date_str}'. Expected 'YYYYMMDDTHH'."
        ) from exc


def model_configs_for_pair(deterministic_model: str, ensemble_model: str):
    try:
        return [MODEL_CONFIGS[deterministic_model], MODEL_CONFIGS[ensemble_model]]
    except KeyError as exc:
        supported = ", ".join(sorted(MODEL_CONFIGS))
        raise ValueError(
            f"Unsupported model {exc.args[0]!r} for AIFS/NeuralGCM blend. "
            f"Supported models: {supported}"
        ) from exc


def check_input_files(model_files: dict[str, Path]) -> bool:
    """
    Check if AIFS and NGCM forecast files exist.
    Returns True if all required files exist, False otherwise.
    """
    for model, path in model_files.items():
        logging.info(f"Checking {model} file: {path}")
        if not path.exists():
            logging.error(f"{model} file not found: {path}")
            return False
        logging.info(f"{model} file found")

    return True


def get_data(
    date: str,
    base: Path,
    active_models,
    model_files: dict[str, Path],
    output_dir: Path | None = None,
    source: str | None = None,
):
    """
    Run all active model processors, merge with climatology, compute weekly
    features, and write all_data.csv.
    """
    logging.info("STEP 6: get_data started")

    if output_dir is not None:
        out_path = output_dir
    elif source == "google":
        out_path = base / "blend" / "output_google" / "india2026" / date / "AIFSv2_NGCM"
    else:
        out_path = base / "blend" / "output" / "india2026" / date / "AIFSv2_NGCM"

    support_dir = base / "blend" / "data" / "india2026" / "shared" / "support"
    shared_coefs_dir = base / "blend" / "data" / "india2026" / "shared" / "coefs"

    thresholds_file = support_dir / "subdistrict_thresholds.csv"
    clim_file = shared_coefs_dir / "subdistrict_full_clim_by_id_time.csv"
    dissemination_file = support_dir / "dissemination_subdistricts.csv"
    exclude_file = support_dir / "subdistricts_to_exclude.csv"

    out_file = out_path / "all_data.csv"

    logging.info(f"  out_path        : {out_path}")
    logging.info(f"  thresholds      : {thresholds_file.exists()}")
    logging.info(f"  clim            : {clim_file.exists()}")
    logging.info(f"  dissemination   : {dissemination_file.exists()}")
    logging.info(f"  exclude         : {exclude_file.exists()}")

    # ── Resolve each active model's NetCDF path ────────────────────────────────
    requested_dt = parse_date(date)
    for cfg in active_models:
        logging.info(
            f"  {cfg.label} file : {model_files[cfg.label]} exists={model_files[cfg.label].exists()}"
        )

    os.makedirs(out_path, exist_ok=True)
    logging.info("STEP 7: output directory created")

    # ── Load shared support data ───────────────────────────────────────────────
    logging.info("STEP 8: loading support data")
    thresholds = pd.read_csv(thresholds_file)
    thresholds.columns = [c.strip() for c in thresholds.columns]
    if (
        "onset_threshold" in thresholds.columns
        and "onset_thresh" not in thresholds.columns
    ):
        thresholds = thresholds.rename(columns={"onset_threshold": "onset_thresh"})

    dissemination_ids = pd.read_csv(dissemination_file)["id"].astype(int).tolist()
    exclude_ids = pd.read_csv(exclude_file)["id"].astype(int).tolist()
    allowed_ids = set(dissemination_ids) - set(exclude_ids)
    logging.info(f"  allowed_ids count: {len(allowed_ids)}")

    if not allowed_ids:
        logging.error("allowed_ids is empty!")
        return None, None

    # ── 1. Process each active model ───────────────────────────────────────────
    logging.info("STEP 9: processing models")
    model_dfs = []
    for cfg in active_models:
        tp_file = model_files.get(cfg.label)
        if tp_file is None:
            logging.warning(f"  WARNING: No file for '{cfg.label}'; skipping.")
            continue
        if not tp_file.exists():
            logging.warning(f"  WARNING: File missing for '{cfg.label}'; skipping.")
            continue
        logging.info(f"  Processing {cfg.label}...")
        df = process_forecast(tp_file, cfg, thresholds, pipeline_date=requested_dt)
        if df.empty:
            logging.warning(f"  WARNING: Empty DataFrame for '{cfg.label}'.")
            continue
        df["time"] = pd.to_datetime(df["time"]).dt.normalize()
        model_dfs.append(df)
        logging.info(f"  {cfg.label} done: {len(df)} records")

    if not model_dfs:
        logging.error("All model processors returned empty results!")
        return None, None

    # ── 2. Merge model outputs ─────────────────────────────────────────────────
    logging.info("STEP 10: merging model outputs")
    merged = model_dfs[0]
    for df in model_dfs[1:]:
        new_cols = ["id", "time", "day"] + [
            c for c in df.columns if c not in merged.columns
        ]
        merged = merged.merge(df[new_cols], on=["id", "time", "day"], how="outer")

    # ── 3. Filter to allowed subdistricts ──────────────────────────────────────
    merged = merged[merged["id"].isin(allowed_ids)].copy()
    logging.info(f"  merged shape: {merged.shape}")

    # ── 4. Compute daily rolling totals ────────────────────────────────────────
    logging.info("STEP 11: computing rolling totals")
    merged = merged.sort_values(["id", "time", "day"])
    rain_cols = [c for c in merged.columns if c.endswith("_rain_daily")]
    logging.info(f"  rain_cols: {rain_cols}")

    for var in rain_cols:
        grp = merged.groupby(["id", "time"])[var]
        merged[f"{var}_5day_total"] = grp.transform(
            lambda x: compute_roll_sum(x.fillna(0).to_numpy(), 5)
        )
        merged[f"{var}_10day_total"] = grp.transform(
            lambda x: compute_roll_sum(x.fillna(0).to_numpy(), 10)
        )

    # ── 5. Bin forecast days into weekly intervals ─────────────────────────────
    logging.info("STEP 12: binning into weekly intervals")
    bins = [0, 7, 14, 21, 28, np.inf]
    labels = ["week1", "week2", "week3", "week4", "later"]
    merged["interval"] = pd.cut(merged["day"], bins=bins, labels=labels, right=True)

    # ── 6. Aggregate ───────────────────────────────────────────────────────────
    logging.info("STEP 13: aggregating")
    agg_dict = {}
    for var in rain_cols:
        agg_dict[var] = "sum"
        agg_dict[f"{var}_5day_total"] = "max"
        agg_dict[f"{var}_10day_total"] = "min"

    agg = (
        merged.groupby(["time", "id", "interval"], observed=False)
        .agg(agg_dict)
        .reset_index()
    )

    # ── 7. Pivot to wide ────────────────────────────────────────────────────────
    logging.info("STEP 14: pivoting to wide")
    wide = agg.pivot_table(
        index=["time", "id"],
        columns="interval",
        values=list(agg_dict.keys()),
        observed=False,
    ).reset_index()

    wide.columns = [
        "_".join(filter(None, (str(c) for c in col))).strip("_")
        for col in wide.columns.values
    ]

    # ── 8. Rename rolling-sum stat columns ─────────────────────────────────────
    for var in rain_cols:
        src = var.replace("_rain_daily", "")
        for wk in labels:
            o5 = f"{var}_5day_total_{wk}"
            n5 = f"max_{src}_5day_{wk}"
            o10 = f"{var}_10day_total_{wk}"
            n10 = f"min_{src}_10day_{wk}"
            if o5 in wide.columns:
                wide = wide.rename(columns={o5: n5})
            if o10 in wide.columns:
                wide = wide.rename(columns={o10: n10})
                wide[n10] = np.power(wide[n10].clip(lower=0.0), 0.25)

    # ── 9. Merge onset thresholds ───────────────────────────────────────────────
    logging.info("STEP 15: merging thresholds")
    wide = wide.merge(thresholds[["id", "onset_thresh"]], on="id", how="left")

    # ── 10. Compute diff features ───────────────────────────────────────────────
    logging.info("STEP 16: computing diff features")
    thresh_rt4 = np.power(wide["onset_thresh"].clip(lower=0.0), 0.25)
    for var in rain_cols:
        src = var.replace("_rain_daily", "")
        for wk in labels:
            max_col = f"max_{src}_5day_{wk}"
            diff_col = f"diff_{src}_{wk}"
            if max_col in wide.columns:
                wide[diff_col] = (
                    np.power(wide[max_col].clip(lower=0.0), 0.25) - thresh_rt4
                )

    # ── 11. Merge climatological weekly probabilities ───────────────────────────
    logging.info("STEP 17: merging climatology")
    clim = pd.read_csv(clim_file)
    clim["id"] = clim["id"].astype(int)

    wide["mm_dd"] = pd.to_datetime(wide["time"]).dt.strftime("%m-%d")
    wide = wide.merge(
        clim,
        left_on=["id", "mm_dd"],
        right_on=["id", "time"],
        how="left",
        suffixes=("", "_clim"),
    )
    if "time_clim" in wide.columns:
        wide = wide.drop(columns=["time_clim"])
    wide = wide.drop(columns=["mm_dd"])

    wide.to_csv(out_file, index=False)
    logging.info(f"Feature table written to {out_file}")

    return out_path, wide


def main():
    logging.info("STEP 1: main() started")

    parser = argparse.ArgumentParser(
        description="Download initial conditions for IFS model"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date to download in format YYYYMMDDTHH. Defaults to latest.",
    )
    parser.add_argument("--deterministic_model", default="AIFS_single_v2")
    parser.add_argument("--ensemble_model", default="NeuralGCM")
    parser.add_argument("--deterministic_input", default=None)
    parser.add_argument("--ensemble_input", default=None)
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--debug", action="store_true", default=False)
    parser.add_argument("--skip_to", type=int, default=None)
    args = parser.parse_args()

    date = args.date

    if not re.match(r"^\d{8}T\d{2}$", date):
        logging.error("Invalid format! Expected: --20260511T00")
        raise ValueError("Invalid date format. Expected 'YYYYMMDDTHH'.")

    base = Path(__file__).resolve().parent.parent.parent.parent.parent
    logging.info(f"STEP 4: base = {base}")
    active_models = model_configs_for_pair(args.deterministic_model, args.ensemble_model)
    model_files = {
        active_models[0].label: Path(args.deterministic_input)
        if args.deterministic_input
        else base / active_models[0].file_template.format(date=date),
        active_models[1].label: Path(args.ensemble_input)
        if args.ensemble_input
        else base / active_models[1].file_template.format(date=date),
    }

    logging.info("STEP 5: checking input files")
    if not check_input_files(model_files):
        logging.error("Required input files missing - pipeline aborted.")
        raise FileNotFoundError("Required input files missing.")

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else base
        / "blend"
        / "output"
        / "india2026"
        / date
        / f"{args.deterministic_model}_{args.ensemble_model}"
    )

    logging.info("STEP 6: running get_data")
    out_path, final = get_data(
        date,
        base,
        active_models,
        model_files,
        output_dir=output_dir,
    )
    if final is None:
        logging.error(f"No data produced for {date} - pipeline aborted.")
        raise ValueError("No data produced by get_data.")

    logging.info("STEP 7: running blend")
    blend(final, out_path)
    logging.info(f"Blend complete for {date}")

    logging.info("STEP 8: running maps")
    make_maps(out_path)

    logging.info(f"--- Pipeline complete for {date} ---")


if __name__ == "__main__":
    main()
