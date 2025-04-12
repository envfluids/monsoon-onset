import requests
import os
import datetime
from tqdm import tqdm  # Install with: pip install tqdm
import logging

# --- Configuration ---
SAVE_DIR = "../raw/ncep_ic/download"  # Directory for downloaded files
# BASE_URL_PATTERN = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gdas.{date}/{cycle}/atmos/gdas.t{cycle}z.atmf000.nc"
BASE_URL_PATTERN = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gdas.{date}/{cycle}/atmos/gdas.t{cycle}z.pgrb2.0p25.f000"
CYCLES_TO_CHECK = 6  # How many past 6-hour cycles to check
CYCLE_HOURS = [18, 12, 6, 0]  # Standard UTC cycle hours
REQUEST_TIMEOUT = 15 # Timeout in seconds for HEAD and GET requests
DOWNLOAD_CHUNK_SIZE = 8192 # Chunk size for downloading

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Functions ---

def get_latest_available_cycle(pattern, num_cycles_to_check, cycles, timeout):
    """
    Checks NOAA NOMADS server backwards from the current time
    to find the latest available GFS/GDAS cycle URL.

    Returns:
        tuple: (url, filename, save_path) or (None, None, None) if not found.
    """
    now_utc = datetime.datetime.utcnow()
    logging.info(f"Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")

    for i in range(num_cycles_to_check):
        # Calculate the time for the cycle we want to check
        approx_cycle_time = now_utc - datetime.timedelta(hours=(now_utc.hour % 6) + (i * 6))

        # Find the closest cycle hour (00, 06, 12, 18) for that time
        check_hour = 0
        for hour in cycles:
             if approx_cycle_time.hour >= hour:
                 check_hour = hour
                 break
        check_dt = datetime.datetime(approx_cycle_time.year, approx_cycle_time.month, approx_cycle_time.day, check_hour, tzinfo=datetime.timezone.utc)

        # Format date and cycle for URL and filename
        date_str = check_dt.strftime("%Y%m%d")
        cycle_str = check_dt.strftime("%H") # Should be '00', '06', '12', or '18'

        # Construct URL and Filename for this cycle
        url = pattern.format(date=date_str, cycle=cycle_str)
        filename = f"gdas_{date_str}T{cycle_str}.pgrb2"
        potential_save_path = os.path.join(SAVE_DIR, filename) # Use global SAVE_DIR

        logging.info(f"Checking for: {url}")
        try:
            # Use HEAD request to check existence without downloading body
            response = requests.head(url, timeout=timeout)
            if response.status_code == 200:
                logging.info(f"Found latest available cycle: {url}")
                return url, filename, potential_save_path
            elif response.status_code == 404:
                logging.debug(f"Not found: {url}") # Use debug for non-critical 'not found'
            else:
                logging.warning(f"Received status code {response.status_code} for {url}")
        except requests.exceptions.Timeout:
            logging.warning(f"Timeout while checking {url}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking URL {url}: {e}")
            # Optional: Implement retry logic or break if connection issue persists

    logging.warning("Could not find any recent available cycle after checking.")
    return None, None, None

def check_local_file_exists(filepath):
    """Checks if a file exists locally."""
    exists = os.path.exists(filepath)
    logging.debug(f"Checking local file: {filepath} - Exists: {exists}")
    return exists

def download_file(url, save_path, filename, chunk_size, timeout):
    """Downloads a file from a URL to a specified path with a progress bar."""
    logging.info(f"Starting download:")
    logging.info(f"  Source: {url}")
    logging.info(f"  Destination: {save_path}")

    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
            total_size = int(response.headers.get('content-length', 0))

            with open(save_path, 'wb') as file, tqdm(
                total=total_size, unit='B', unit_scale=True, desc=filename, leave=False # leave=False cleans up bar on completion
            ) as bar:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:  # filter out keep-alive new chunks
                        file.write(chunk)
                        bar.update(len(chunk))

        # Verify download size if possible
        if total_size != 0 and os.path.getsize(save_path) != total_size:
             logging.error(f"Download incomplete: Expected {total_size} bytes, got {os.path.getsize(save_path)}")
             # Optionally remove incomplete file here
             # os.remove(save_path)
             return False # Indicate failure

        logging.info(f"Download complete: {save_path}")
        return True # Indicate success

    except requests.exceptions.Timeout:
        logging.error(f"Timeout during download from {url}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during download from {url}: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during download: {e}")

    # Clean up potentially incomplete file if any error occurred
    if os.path.exists(save_path):
        try:
            logging.info(f"Removing potentially incomplete file: {save_path}")
            os.remove(save_path)
        except OSError as oe:
            logging.error(f"Error removing incomplete file {save_path}: {oe}")
    return False # Indicate failure

def ensure_directory_exists(dir_path):
    """Creates the directory if it doesn't exist."""
    if not os.path.exists(dir_path):
        logging.info(f"Creating directory: {dir_path}")
        try:
            os.makedirs(dir_path, exist_ok=True) # exist_ok=True handles race conditions
        except OSError as e:
            logging.error(f"Failed to create directory {dir_path}: {e}")
            return False
    return True

# --- Main Orchestration ---

def get_data():
    """Main function to find, check, and download the latest IC file."""
    logging.info("--- Starting Initial Conditions Check ---")

    # 1. Find the latest available cycle URL and target paths
    latest_url, latest_filename, latest_save_path = get_latest_available_cycle(
        BASE_URL_PATTERN, CYCLES_TO_CHECK, CYCLE_HOURS, REQUEST_TIMEOUT
    )

    if not latest_url:
        logging.warning("No suitable initial conditions file found on the server.")
        logging.info("--- Check Complete (No Action Taken) ---")
        return # Exit if no file found

    # 2. Ensure the save directory exists before checking/downloading
    if not ensure_directory_exists(SAVE_DIR):
        logging.error("Cannot proceed without save directory.")
        logging.info("--- Check Complete (Error) ---")
        return # Exit if directory can't be created

    # 3. Check if the specific file already exists locally
    if check_local_file_exists(latest_save_path):
        logging.info(f"Latest available file already downloaded: {latest_save_path}")
        logging.info("--- Check Complete (Already Present) ---")
    else:
        # 4. Download the file if it doesn't exist locally
        logging.info(f"Latest available file not found locally. Attempting download.")
        success = download_file(latest_url, latest_save_path, latest_filename, DOWNLOAD_CHUNK_SIZE, REQUEST_TIMEOUT)
        if success:
             logging.info("--- Check Complete (Downloaded Successfully) ---")
             latest_time = latest_filename.split('.')[0].split('_')[1]
             return latest_time
        else:
             logging.error("Download failed.")
             logging.info("--- Check Complete (Download Failed) ---")


if __name__ == "__main__":
    get_data()