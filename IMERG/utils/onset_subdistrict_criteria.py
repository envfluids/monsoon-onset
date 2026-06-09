import logging
import os
import argparse
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


root_dir = Path(__file__).resolve().parents[1]
india_data_dir = root_dir / "data" / "india"
log_dir = root_dir / "logs"
os.makedirs(log_dir, exist_ok=True)
india_data_dir.mkdir(parents=True, exist_ok=True)

subdistrict_precip_csv = india_data_dir / "daily_rainfall_subdistricts.csv"
criterion1_csv = india_data_dir / "criterion1_subdistricts.csv"
criterion2_csv = india_data_dir / "criterion2_subdistricts.csv"
criterion3_csv = india_data_dir / "criterion3_subdistricts.csv"
threshold_array_file = india_data_dir / "threshold_array.npy"

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    log_file = log_dir / "onset_subdistrict_criteria.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.FileHandler(log_file, mode='a')]
    )


def check_dry_spell(df, dates2):
    rain_df = (
        df[df["Date"].astype(str).isin([d.strftime('%Y%m%d') for d in dates2])]
        .drop(columns=["Date"])
        .reset_index(drop=True)
    )
    
    dry_spell_criterion = np.zeros(rain_df.shape[1], dtype=bool)  # numpy array instead
    for i in range(len(rain_df) - 9):
        window_sum = rain_df.iloc[i:i+10].sum(axis=0).values  # .values here
        dry_spell_criterion = dry_spell_criterion | (window_sum <= 5)
    
    return dry_spell_criterion


def criterion_one(date):
    date = datetime.strptime(date, "%Y%m%d")
    logger.info(f"Evaluating Criterion 1 for date: {date}")
    dates = [date + timedelta(days=i) for i in range(5)]

    if not subdistrict_precip_csv.exists():
        logger.error(f"Subdistrict precipitation CSV file not found at: {subdistrict_precip_csv}")
        return False

    df = pd.read_csv(subdistrict_precip_csv)
    logger.info(f"Loaded subdistrict precipitation data with shape: {df.shape}")

    if not all(d.strftime('%Y%m%d') in df["Date"].astype(str).values for d in dates):
        logger.error(f"One or more required dates not found in CSV: {[d.strftime('%Y%m%d') for d in dates]}")
        return False

    if not criterion1_csv.exists():
        logger.info(f"Criterion 1 CSV file not found at: {criterion1_csv}. Creating a new one.")
        criterion1_df = pd.DataFrame(columns=df.columns)
        criterion1_df.to_csv(criterion1_csv, index=False)

    criterion1_df = pd.read_csv(criterion1_csv)
    logger.info(f"Loaded Criterion 1 CSV file with shape: {criterion1_df.shape}")

    if not threshold_array_file.exists():
        logger.error(f"Threshold array file not found at: {threshold_array_file}")
        return False
    threshold_array = np.load(threshold_array_file)
    logger.info(f"Loaded threshold array with shape: {threshold_array.shape}")

    # First day criterion — rainfall > 1mm on the given date
    first_day_criterion = (
        df[df["Date"].astype(str) == date.strftime('%Y%m%d')]
        .drop(columns=["Date"])
        .values.flatten() > 1
    )
    logger.info(f"First day criterion calculated. True count: {first_day_criterion.sum()}")

    # 5 day sum criterion — sum of rainfall over 5 days > threshold
    five_day_sum_criterion = (
        df[df["Date"].astype(str).isin([d.strftime('%Y%m%d') for d in dates])]
        .drop(columns=["Date"])
        .sum(axis=0)
        > threshold_array
    )
    logger.info(f"5 day sum criterion calculated. True count: {five_day_sum_criterion.sum()}")

    # Both criteria must be met
    criterion1 = first_day_criterion & five_day_sum_criterion.values
    logger.info(f"Criterion 1 calculated. True count: {criterion1.sum()}")

    # Build new row
    new_row = [date.strftime("%Y%m%d")] + criterion1.astype(int).tolist()
    date_str = date.strftime("%Y%m%d")

    # Replace if date already exists
    if int(date_str) in criterion1_df["Date"].values or date_str in criterion1_df["Date"].astype(str).values:
        logger.info(f"Date {date_str} already exists in Criterion 1 CSV, replacing it.")
        criterion1_df = criterion1_df[criterion1_df["Date"].astype(str) != date_str]

    # Append new row
    criterion1_df = pd.concat(
        [criterion1_df, pd.DataFrame([new_row], columns=criterion1_df.columns)],
        ignore_index=True
    )

    # Sort by date and save
    criterion1_df["Date"] = pd.to_datetime(criterion1_df["Date"], format="%Y%m%d")
    criterion1_df = criterion1_df.sort_values("Date", ascending=False)
    criterion1_df["Date"] = criterion1_df["Date"].dt.strftime("%Y%m%d")
    criterion1_df.to_csv(criterion1_csv, index=False)
    logger.info(f"Updated Criterion 1 CSV file with new values for date: {date}")
    return True


def criterion_two(date):
    date = datetime.strptime(date, "%Y%m%d")
    logger.info(f"Evaluating Criterion 2 for date: {date}")
    dates = [date + timedelta(days=i) for i in range(5)]

    if not subdistrict_precip_csv.exists():
        logger.error(f"Subdistrict precipitation CSV file not found at: {subdistrict_precip_csv}")
        return False

    df = pd.read_csv(subdistrict_precip_csv)
    logger.info(f"Loaded subdistrict precipitation data with shape: {df.shape}")

    if not all(d.strftime('%Y%m%d') in df["Date"].astype(str).values for d in dates):
        logger.error(f"One or more required dates not found in CSV: {[d.strftime('%Y%m%d') for d in dates]}")
        return False
    

    latest_date = datetime.strptime(df["Date"].astype(str).iloc[0], "%Y%m%d")
    start = date + timedelta(days=5)
    dates2 = [start + timedelta(days=i) for i in range((latest_date - start).days + 1)]

    if len(dates2) < 10:
        logger.error(f"Not enough future dates available in CSV for Criterion 2.")
        return False

    if not criterion2_csv.exists():
        logger.info(f"Criterion 2 CSV file not found at: {criterion2_csv}. Creating a new one.")
        criterion2_df = pd.DataFrame(columns=df.columns)
        criterion2_df.to_csv(criterion2_csv, index=False)

    criterion2_df = pd.read_csv(criterion2_csv)
    logger.info(f"Loaded Criterion 2 CSV file with shape: {criterion2_df.shape}")

    if not threshold_array_file.exists():
        logger.error(f"Threshold array file not found at: {threshold_array_file}")
        return False
    threshold_array = np.load(threshold_array_file)
    logger.info(f"Loaded threshold array with shape: {threshold_array.shape}")

    # First day criterion — rainfall > 1mm on the given date
    first_day_criterion = (
        df[df["Date"].astype(str) == date.strftime('%Y%m%d')]
        .drop(columns=["Date"])
        .values.flatten() > 1
    )
    logger.info(f"First day criterion calculated. True count: {first_day_criterion.sum()}")

    # 5 day sum criterion — sum of rainfall over 5 days > threshold
    five_day_sum_criterion = (
        df[df["Date"].astype(str).isin([d.strftime('%Y%m%d') for d in dates])]
        .drop(columns=["Date"])
        .sum(axis=0)
        > threshold_array
    )
    logger.info(f"5 day sum criterion calculated. True count: {five_day_sum_criterion.sum()}")


    dry_spell_criterion = check_dry_spell(df, dates2)
    logger.info(f"Dry spell criterion calculated. True count: {dry_spell_criterion.sum()}")

    # Both criteria must be met
    criterion2 = first_day_criterion & five_day_sum_criterion.values & ~dry_spell_criterion
    logger.info(f"Criterion 2 calculated. True count: {criterion2.sum()}")

    # Build new row
    new_row = [date.strftime("%Y%m%d")] + criterion2.astype(int).tolist()
    date_str = date.strftime("%Y%m%d")

    # Replace if date already exists
    if int(date_str) in criterion2_df["Date"].values or date_str in criterion2_df["Date"].astype(str).values:
        logger.info(f"Date {date_str} already exists in Criterion 2 CSV, replacing it.")
        criterion2_df = criterion2_df[criterion2_df["Date"].astype(str) != date_str]

    # Append new row
    criterion2_df = pd.concat(
        [criterion2_df, pd.DataFrame([new_row], columns=criterion2_df.columns)],
        ignore_index=True
    )

    # Sort by date and save
    criterion2_df["Date"] = pd.to_datetime(criterion2_df["Date"], format="%Y%m%d")
    criterion2_df = criterion2_df.sort_values("Date", ascending=False)
    criterion2_df["Date"] = criterion2_df["Date"].dt.strftime("%Y%m%d")
    criterion2_df.to_csv(criterion2_csv, index=False)
    logger.info(f"Updated Criterion 2 CSV file with new values for date: {date}")
    return True

def criterion_three(date):
    date = datetime.strptime(date, "%Y%m%d")
    logger.info(f"Evaluating Criterion 3 for date: {date}")
    dates = [date + timedelta(days=i) for i in range(5)]

    if not subdistrict_precip_csv.exists():
        logger.error(f"Subdistrict precipitation CSV file not found at: {subdistrict_precip_csv}")
        return False

    df = pd.read_csv(subdistrict_precip_csv)
    logger.info(f"Loaded subdistrict precipitation data with shape: {df.shape}")

    if not all(d.strftime('%Y%m%d') in df["Date"].astype(str).values for d in dates):
        logger.error(f"One or more required dates not found in CSV: {[d.strftime('%Y%m%d') for d in dates]}")
        return False
    

    latest_date = date + timedelta(days=30)
    start = date + timedelta(days=5)
    dates2 = [start + timedelta(days=i) for i in range((latest_date - start).days + 1)]

    if not criterion3_csv.exists():
        logger.info(f"Criterion 3 CSV file not found at: {criterion3_csv}. Creating a new one.")
        criterion3_df = pd.DataFrame(columns=df.columns)
        criterion3_df.to_csv(criterion3_csv, index=False)

    criterion3_df = pd.read_csv(criterion3_csv)
    logger.info(f"Loaded Criterion 3 CSV file with shape: {criterion3_df.shape}")

    if not threshold_array_file.exists():
        logger.error(f"Threshold array file not found at: {threshold_array_file}")
        return False
    threshold_array = np.load(threshold_array_file)
    logger.info(f"Loaded threshold array with shape: {threshold_array.shape}")

    # First day criterion — rainfall > 1mm on the given date
    first_day_criterion = (
        df[df["Date"].astype(str) == date.strftime('%Y%m%d')]
        .drop(columns=["Date"])
        .values.flatten() > 1
    )
    logger.info(f"First day criterion calculated. True count: {first_day_criterion.sum()}")

    # 5 day sum criterion — sum of rainfall over 5 days > threshold
    five_day_sum_criterion = (
        df[df["Date"].astype(str).isin([d.strftime('%Y%m%d') for d in dates])]
        .drop(columns=["Date"])
        .sum(axis=0)
        > threshold_array
    )
    logger.info(f"5 day sum criterion calculated. True count: {five_day_sum_criterion.sum()}")


    dry_spell_criterion = check_dry_spell(df, dates2)
    logger.info(f"Dry spell criterion calculated. True count: {dry_spell_criterion.sum()}")

    # Both criteria must be met
    criterion3 = first_day_criterion & five_day_sum_criterion.values & ~dry_spell_criterion
    logger.info(f"Criterion 3 calculated. True count: {criterion3.sum()}")

    # Build new row
    new_row = [date.strftime("%Y%m%d")] + criterion3.astype(int).tolist()
    date_str = date.strftime("%Y%m%d")

    # Replace if date already exists
    if int(date_str) in criterion3_df["Date"].values or date_str in criterion3_df["Date"].astype(str).values:
        logger.info(f"Date {date_str} already exists in Criterion 3 CSV, replacing it.")
        criterion3_df = criterion3_df[criterion3_df["Date"].astype(str) != date_str]

    # Append new row
    criterion3_df = pd.concat(
        [criterion3_df, pd.DataFrame([new_row], columns=criterion3_df.columns)],
        ignore_index=True
    )

    # Sort by date and save
    criterion3_df["Date"] = pd.to_datetime(criterion3_df["Date"], format="%Y%m%d")
    criterion3_df = criterion3_df.sort_values("Date", ascending=False)
    criterion3_df["Date"] = criterion3_df["Date"].dt.strftime("%Y%m%d")
    criterion3_df.to_csv(criterion3_csv, index=False)
    logger.info(f"Updated Criterion 3 CSV file with new values for date: {date}")
    return True

def run_criteria(date: str) -> dict[str, bool]:
    return {
        "criterion1": criterion_one(date),
        "criterion2": criterion_two(date),
        "criterion3": criterion_three(date),
    }


def main():
    configure_logging()
    parser = argparse.ArgumentParser(description="Evaluate IMD subdistrict onset criteria.")
    parser.add_argument("date", type=str, help="Date in YYYYMMDD format")
    args = parser.parse_args()
    results = run_criteria(args.date)
    if not all(results.values()):
        failed = ", ".join(name for name, ok in results.items() if not ok)
        logger.warning("Some onset criteria were not updated: %s", failed)


if __name__ == "__main__":
    main()
