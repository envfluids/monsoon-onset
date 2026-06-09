import pickle
import xarray as xr
import argparse
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np
import logging
import os

try:
    from .create_subdistrict_matrix import ensure_matrices
    from .plot_imd_rainfall import plot_imd_daily_precip_subdistrict
except ImportError:
    from create_subdistrict_matrix import ensure_matrices
    from plot_imd_rainfall import plot_imd_daily_precip_subdistrict


imerg_root = Path(__file__).resolve().parents[1]
india_data_dir = imerg_root / "data" / "india"
imd_observations_path = imerg_root / "raw" / "IMD"
log_dir = imerg_root / "logs"
os.makedirs(log_dir, exist_ok=True)
india_data_dir.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger(__name__)


def configure_logging() -> None:
    log_file = log_dir / "get_subdistrict_rainfall.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.FileHandler(log_file, mode='a')]
    )



def create_subdistrict_csv(csv_file, target_to_index):
    logger.info(f"Creating subdistrict CSV file at: {csv_file}")
    columns = ["Date"] + [str(k) for k, v in sorted(target_to_index.items(), key=lambda x: x[1])]
    df = pd.DataFrame(columns=columns)    
    # Save the empty DataFrame to a CSV file
    df.to_csv(csv_file, index=False)
    logger.info(f"Created empty CSV file at: {csv_file}")

def get_rainfall_from_lat_lon(imd_data, source):
    lat, lon = map(float, source.split("_"))
    lat_idx = np.argmin(np.abs(imd_data["lat"].values - lat))
    lon_idx = np.argmin(np.abs(imd_data["lon"].values - lon))
    rainfall_value = imd_data["rain"][0, lat_idx, lon_idx].item()
    return rainfall_value

def convert_gridded_to_subdistrict(date, source_to_index, target_to_index, weight_matrix):
    logger.info(f"Converting gridded data to subdistrict level for date: {date}")
    # Load the IMD data for the given date
    imd_file = imd_observations_path / f"{date.strftime('%Y%m%d')}.nc4"
    if not imd_file.exists():
        logger.error(f"IMD data file not found for date: {date} at path: {imd_file}")
        return 0
    imd_data = xr.open_dataset(imd_file)
    logger.info(f"Loaded IMD data for {date}")

    # check if csv file exists
    csv_file = india_data_dir / "daily_rainfall_subdistricts.csv"
    if not csv_file.exists():
        logger.info(f"CSV file not found at: {csv_file}. Creating a new one.")
        create_subdistrict_csv(csv_file,target_to_index)
    df = pd.read_csv(csv_file)
    logger.info(f"Loaded subdistrict CSV file with shape: {df.shape}")
    # Convert gridded data to subdistrict level using the weight matrix
    grid_ranfall_values = []
    for source, _ in source_to_index.items():
        rainfall_value = get_rainfall_from_lat_lon(imd_data, source)
        grid_ranfall_values.append(rainfall_value)
    logger.info(f"Extracted rainfall values for all grid points. Total points: {len(grid_ranfall_values)}")
    grid_rainfall_values = np.array(grid_ranfall_values)
    logger.info(f"Converted to numpy array with shape: {grid_rainfall_values.shape}")
    subdistrict_rainfall = weight_matrix @ grid_rainfall_values
    logger.info(f"Calculated subdistrict rainfall values with shape: {subdistrict_rainfall.shape}")
    # Update the DataFrame with the new rainfall values
    new_row = [date.strftime("%Y%m%d")] + subdistrict_rainfall.tolist()
    date_str = date.strftime("%Y%m%d")
    if int(date_str) in df["Date"].values or date_str in df["Date"].astype(str).values:
        logger.info(f"Date {date_str} already exists in CSV, replacing it.")
        df = df[df["Date"].astype(str) != date_str]
    
    df.loc[len(df)] = new_row
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d")
    df = df.sort_values("Date", ascending=False)
    df["Date"] = df["Date"].dt.strftime("%Y%m%d")
    df.to_csv(csv_file, index=False)
    return 1

    




def process_subdistrict_rainfall(date: datetime, output_dir: Path | None = None) -> bool:
    logger.info(f"Processing date: {date}")
    ensure_matrices()

    source_to_index_path = india_data_dir / "source_to_index.pkl"
    with open(source_to_index_path, "rb") as f:
        source_to_index = pickle.load(f)
    
    target_to_index_path = india_data_dir / "target_to_index.pkl"
    with open(target_to_index_path, "rb") as f:
        target_to_index = pickle.load(f)

    weight_matrix_path = india_data_dir / "weight_matrix.npy"
    weight_matrix = np.load(weight_matrix_path)
    result = convert_gridded_to_subdistrict(date, source_to_index, target_to_index, weight_matrix)
    
    if result == 1:
        logger.info(f"Successfully processed date: {date}")
        plot_imd_daily_precip_subdistrict(date, output_dir=output_dir)
        return True
    else:
        logger.error(f"Failed to process date: {date}")
        return False


def main():
    configure_logging()
    parser = argparse.ArgumentParser(description="Get the rainfall date")
    parser.add_argument("date", type=str, help="Date in YYYYMMDD format")
    parser.add_argument("--output-dir", type=Path, help="Directory for subdistrict maps")
    args = parser.parse_args()
    date = datetime.strptime(args.date, "%Y%m%d")
    process_subdistrict_rainfall(date, output_dir=args.output_dir)
    


if __name__ == "__main__":
    main()
