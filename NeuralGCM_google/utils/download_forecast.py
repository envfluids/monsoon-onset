import gcsfs
import os
from pathlib import Path
from datetime import datetime, timezone
import xarray as xr
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

def get_latest_new_timestamp(bucket_path: str, token_path: str, local_download_dir: str):
    """
    Checks GCS for the latest forecast timestamp and returns it if the
    corresponding .zarr file is not on the local disk.

    Args:
        bucket_path (str): The GCS path to the directory containing forecasts.
                           Example: "gs://neuralgcm-monsoon-realtime/prod/stochastic_precip_2_8_deg/"
        token_path (str): The local path to the GCS ADC JSON token file.
                          Example: "/Users/marchakitus/Downloads/adc.json"
        local_download_dir (str): The local directory where .zarr files are (or would be) stored.
                                   Example: "/data/downloaded_forecasts/"

    Returns:
        str or None: The latest UNIX timestamp (as a string) if its .zarr file
                     is not found in local_download_dir. Returns None if no new
                     forecast needs downloading (either none on GCS or latest
                     is already local).
    """
    try:
        fs = gcsfs.GCSFileSystem(token=token_path)
    except Exception as e:
        logging.error(f"Error initializing GCSFileSystem: {e}")
        return None

    try:
        gcs_dir_path = bucket_path.rstrip('/') + '/'
        all_gcs_files = fs.ls(gcs_dir_path)
    except Exception as e:
        logging.error(f"Error listing files in GCS bucket {bucket_path}: {e}")
        return None

    done_timestamps = []
    for gcs_file_path in all_gcs_files:
        filename = os.path.basename(gcs_file_path)
        if filename.endswith(".zarr-done"):
            try:
                timestamp_str = filename.replace(".zarr-done", "")
                int(timestamp_str)  # Validate that the timestamp part is an integer
                done_timestamps.append(timestamp_str)
            except ValueError:
                # Silently skip files with non-integer timestamps before .zarr-done
                continue

    if not done_timestamps:
        # No .zarr-done files found, or none had valid timestamps
        return None

    # Determine the latest timestamp from GCS
    # max() on list of strings will work lexicographically;
    # to ensure numerical maximum, convert to int for key.
    latest_gcs_timestamp_str = max(done_timestamps, key=int)

    date = datetime.fromtimestamp(int(latest_gcs_timestamp_str)).astimezone(timezone.utc)
    date_f = date.strftime('%Y%m%dT%H')
    logging.info(f"Latest GCS timestamp: {latest_gcs_timestamp_str}; ({date_f})")
    # Check if the corresponding .zarr file exists locally
    local_target_filename = f"{date_f}.nc"
    local_target_filepath = os.path.join(local_download_dir, local_target_filename)

    if os.path.exists(local_target_filepath):
        # File already exists locally
        return None, None
    else:
        # File does not exist locally, so this timestamp is "new"
        return latest_gcs_timestamp_str, date_f


def get_zarr(bucket_path: str, token_path: str, local_download_dir: str, timestamp: str, date_f: str):
    """
    Downloads the .zarr file corresponding to the given timestamp from GCS
    to the local directory.

    Args:
        bucket_path (str): The GCS path to the directory containing forecasts.
        token_path (str): The local path to the GCS ADC JSON token file.
        local_download_dir (str): The local directory where .zarr files should be stored.
        timestamp (str): The UNIX timestamp string for which to download the .zarr file.
        date_f (str): The formatted date string for the timestamp, used in the output filename.

    Returns:
        bool: True if download was successful, False otherwise.
    """
    zarr_path = bucket_path + f"{timestamp}.zarr"
    ds = xr.open_zarr(zarr_path, consolidated=True, storage_options={'token': token_path}, decode_timedelta=True)
    ds = ds[['total_precipitation']].isel(surface=0)
    ds.to_netcdf(
        os.path.join(local_download_dir, f"{date_f}.nc"),
    )


def get_forecast():
    gcs_bucket_path_config = "gs://neuralgcm-monsoon-realtime/prod/stochastic_precip_2_8_deg/"
    base = Path(__file__).resolve().parent.parent
    # IMPORTANT: Update this to your actual GCS token file path
    # The example path you provided was "/Users/marchakitus/Downloads/adc.json"
    gcs_token_path_config = base / ".auth" / "adc.json"  # <--- CHANGE THIS
    print(f"Using GCS token path: {gcs_token_path_config}")

    # IMPORTANT: Update this to your local directory for storing/checking forecasts
    local_forecasts_dir_config = base / "raw"

    logging.info("Checking for new forecast")
    logging.info(f"GCS Bucket: {gcs_bucket_path_config}")
    logging.info(f"Token Path: {gcs_token_path_config}")
    logging.info(f"Local Download Directory: {local_forecasts_dir_config}")


    new_timestamp_to_download, date_f = get_latest_new_timestamp(
        gcs_bucket_path_config,
        str(gcs_token_path_config),
        local_forecasts_dir_config
    )

    if new_timestamp_to_download:
        logging.info(f"New forecast timestamp: {new_timestamp_to_download} ({date_f})")
        logging.info(f"Downloading {date_f}.nc from GCS...")
        try:
            get_zarr(
                gcs_bucket_path_config,
                str(gcs_token_path_config),
                local_forecasts_dir_config,
                new_timestamp_to_download,
                date_f
            )
        except Exception as e:
            logging.error(f"Error downloading {date_f}.nc: {e}")
        return date_f
        
    else:
        logging.info("No new forecast timestamp to download.")
        return None

if __name__ == "__main__":
    get_forecast()