import logging
from pathlib import Path
import datetime
# The ecmwfapi library is deprecated. The new library is `ecmwf-api-client`.
# The code will assume the client is configured with the necessary API key.
from ecmwfapi import ECMWFDataServer

# --- Setup robust logging ---
# This provides clear, timestamped output to understand the script's execution.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def download_forecast(date: str, raw_data_path: Path, server: ECMWFDataServer):
    """
    Downloads control ('cf') and perturbed ('pf') forecasts for a specific date.

    This function checks if files already exist and only downloads what is missing for the given date.
    It handles both forecast types in a single, resilient pass.

    Args:
        date: The forecast initialization date in 'YYYY-MM-DD' format.
        raw_data_path: The Path object pointing to the directory for saving GRIB files.
        server: An initialized ECMWFDataServer instance.

    Returns:
        bool: True if data for the date was successfully downloaded or already exists.
              The function will raise an exception from the API call on failure.
    """
    logging.info(f"Processing forecast data for date: {date}")
    types_to_check = ["cf", "pf"]
    date_formatted = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%dT00")

    # --- Loop through both Control and Perturbed forecast types ---
    for f_type in types_to_check:
        target_file = raw_data_path / f"ifs_s2s_{f_type}_init_{date_formatted}.grib"

        # --- FIX 1: Check for existing files without raising an error ---
        # If a file exists, we log it and move on. No need to stop execution.
        if target_file.exists():
            logging.info(f"File {target_file.name} already exists. Skipping download.")
            continue

        logging.info(f"Downloading {f_type} forecast for date {date} to {target_file.name}")

        # --- FIX 2: Define the request for each specific file type ---
        # The base request is the same for both, but 'number' is only for 'pf'.
        request = {
            "class": "s2",
            "dataset": "s2s",
            "date": date,
            "expver": "prod",
            "levtype": "sfc",
            "model": "glob",
            "origin": "ecmf",
            "param": "228228",  # Total precipitation
            # This simplified syntax is more readable than listing every step.
            "step": "0/to/1104/by/6",
            "stream": "enfo",
            "time": "00:00:00",
            "type": f_type,
            "target": str(target_file),
        }

        # S2S perturbed forecasts have 50 members. This is added only for the 'pf' type.
        if f_type == "pf":
            request["number"] = "1/to/50"

        # The actual API call to retrieve the data
        server.retrieve(request)
        logging.info(f"Successfully downloaded {target_file.name}")

    # If the function completes without API errors, the date is considered successful.
    return True


def get_data(max_retries: int = 3):
    """
    Attempts to download the latest available forecast, trying recent days.

    It starts with yesterday and works backwards for a specified number of days.

    Args:
        max_retries: The maximum number of past days to check for data.

    Returns:
        str | None: The date string ('YYYYMMDDTHH') of the successfully downloaded
                    forecast, or None if no data could be retrieved.
    """
    # Define paths relative to the script's location
    base_path = Path(__file__).resolve().parent.parent
    raw_data_path = base_path / "raw" / "grib"
    raw_data_path.mkdir(parents=True, exist_ok=True)

    try:
        server = ECMWFDataServer()
    except Exception as e:
        logging.error(f"Failed to initialize ECMWFDataServer. Check API key. Error: {e}")
        return None

    # --- FIX 3: Replace repeated try/except blocks with a clean loop ---
    # This loop attempts to download data, going back one day at a time.
    for days_ago in range(1, max_retries + 1):
        target_date = datetime.datetime.now() - datetime.timedelta(days=days_ago)
        date_str = target_date.strftime("%Y-%m-%d")

        try:
            # Attempt to download all necessary files for the target date
            success = download_forecast(date_str, raw_data_path, server)
            if success:
                logging.info(f"Data acquisition successful for date: {date_str}")
                # Return the date in the originally desired format and exit
                return target_date.strftime("%Y%m%dT00")
        except Exception as e:
            # This will catch errors from the API (e.g., data not yet available)
            logging.warning(f"Could not retrieve data for {date_str}. Error: {e}")
            logging.info("Trying previous day...")

    logging.error(f"Failed to download forecast after trying the last {max_retries} days.")
    return None


if __name__ == "__main__":
    logging.info("Starting ECMWF forecast data acquisition process...")
    # The return value is the date string (e.g., '20250615T00') or None
    latest_forecast_date = get_data()

    if latest_forecast_date:
        print(f"\nProcess complete. Latest forecast date processed: {latest_forecast_date}")
    else:
        print("\nProcess failed. Could not retrieve any forecast data.")
