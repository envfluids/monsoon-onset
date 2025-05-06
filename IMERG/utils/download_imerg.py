import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

def get_data(max_days_to_check=7):
    """
    Checks for the latest available IMERG daily data from NASA GPM, downloads it 
    if not already present, and saves it to the specified raw data directory.

    This script uses wget for downloads and requires .urs_cookies for NASA authentication.
    It assumes a directory structure where this script might be in 'utils',
    and data/auth paths are relative to the parent of 'utils'.

    Args:
        max_days_to_check (int): Maximum number of past days to check for data.
                                 IMERG data usually has a delay, so checking a few
                                 past days is necessary.

    Returns:
        str: The date string ('YYYYMMDD') of the downloaded data if a new file is downloaded.
        None: If the latest available data is already downloaded, no data is found
              within the checked range, or an error occurs.
    """
    try:
        # Determine paths relative to this script file
        # Assumes this script is in a 'utils' directory, and 'raw' and '.auth' are siblings of 'utils'
        script_file_path = Path(__file__).resolve() # a/b/utils/imerg_downloader.py
        utils_dir = script_file_path.parent       # a/b/utils
        base_dir = utils_dir.parent               # a/b (project root)
    except NameError: 
        # Fallback for interactive environments where __file__ might not be defined
        # In this case, assume current working directory is project root or utils
        # For robustness, it's better if this script is part of a package or has a fixed structure.
        # If running interactively or __file__ is not set, assume script is in 'utils' relative to current execution
        # This might need adjustment based on how it's run.
        # For a robust solution, explicitly pass base_dir or ensure __file__ is available.
        # For now, let's assume a structure where 'utils' is a subdir of where we want 'raw' and '.auth'
        # If current dir is 'utils', parent is project root. If current dir is project root, this is okay too.
        
        # Check if we are in 'utils' directory
        if Path.cwd().name == 'utils':
            base_dir = Path.cwd().parent
        else: # Assume cwd is project root
            base_dir = Path.cwd()
        logging.warning(f"Warning: __file__ not defined. Assuming base directory: {base_dir}")


    raw_dir_relative_to_base = Path("raw") / "IMERG_daily"
    auth_dir_relative_to_base = Path(".auth")
    
    raw_dir = base_dir / raw_dir_relative_to_base
    cookies_file = base_dir / auth_dir_relative_to_base / ".urs_cookies"

    # Ensure the target directory for raw data exists
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error(f"Error creating directory {raw_dir}: {e}")
        return None

    if not cookies_file.exists():
        logging.error(f"Ensure the .urs_cookies file is present at {cookies_file}")
        return None

    # IMERG data is often referenced in UTC. Start checking from 1 day ago UTC.
    # The original script used `date -d yesterday`. Depending on server update times,
    # data for "yesterday" (UTC) might appear with a delay.
    current_date_utc = datetime.now(timezone.utc)

    for i in range(0, max_days_to_check + 1):
        target_date = current_date_utc - timedelta(days=i)
        year = target_date.strftime("%Y")
        month = target_date.strftime("%m")
        day = target_date.strftime("%d")
        date_str_compact = f"{year}{month}{day}"

        # Construct filename and URL (as per GPM_3IMERGDL.07)
        # Example filename: 3B-DAY-L.MS.MRG.3IMERG.20230501-S000000-E235959.V07B.nc4
        filename = f"3B-DAY-L.MS.MRG.3IMERG.{date_str_compact}-S000000-E235959.V07B.nc4"
        url = f"https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/GPM_3IMERGDL.07/{year}/{month}/{filename}"
        
        local_file_path = raw_dir / filename

        logging.info(f"Checking for data from {date_str_compact}...")
        logging.info(f"Remote URL: {url}")
        logging.info(f"Local path: {local_file_path}")
        
        # Check if the file exists remotely using wget --spider
        spider_command = [
            "wget",
            "--spider",
            f"--load-cookies={cookies_file}",
            f"--save-cookies={cookies_file}", # Recommended to keep session active
            "--keep-session-cookies",
            url
        ]

        try:
            spider_result = subprocess.run(spider_command, capture_output=True, text=True, check=False, timeout=60)
            
            if spider_result.returncode == 0:
                logging.info(f"Remote file found for {date_str_compact} ({filename}).")
                
                if local_file_path.exists():
                    logging.info(f"File {filename} already exists locally. No download needed.")
                    return None # Latest available is already downloaded
                else:
                    logging.info(f"Downloading {filename} to {raw_dir}...")
                    download_command = [
                        "wget",
                        f"--load-cookies={cookies_file}",
                        f"--save-cookies={cookies_file}",
                        "--keep-session-cookies",
                        "--content-disposition", # Crucial for NASA GES DISC downloads
                        url,
                        "-P", str(raw_dir) # Download to the raw_dir directory
                    ]
                    
                    download_result = subprocess.run(download_command, capture_output=False, text=False, check=False, timeout=300)

                    if download_result.returncode == 0:
                        # Verify the file was downloaded. '--content-disposition' should use the server-provided name,
                        # which is expected to be 'filename'.
                        if (raw_dir / filename).exists():
                            logging.info(f"Downloaded {filename} to {raw_dir}.")
                            return date_str_compact
                        else:
                            # This might happen if content-disposition provides a different name than expected,
                            # or if there was an issue with -P.
                            logging.warning(f"Downloaded file not found as {filename}. Checking raw_dir for any files with date string.")
                            # logging.info(f"         STDOUT: {download_result.stdout.strip()}")
                            # logging.info(f"         STDERR: {download_result.stderr.strip()}")
                            # Attempt to find a file with the date string as a fallback
                            downloaded_files = list(raw_dir.glob(f"*{date_str_compact}*.nc4"))
                            if downloaded_files:
                                actual_file = downloaded_files[0]
                                logging.info(f"INFO: Found potential match: {actual_file.name}. Assuming this is the downloaded file.")
                                # If you want to be strict and ensure it IS the filename, add a check here.
                                return date_str_compact # Return the date, as something for that date was downloaded.
                            else:
                                logging.error(f"Downloaded file not found as {filename} and no other files with date string found.")
                                return None 
                    else:
                        logging.error(f"Failed to download {filename}. wget exit code: {download_result.returncode}")
                        # logging.error(f"         STDOUT: {download_result.stdout.strip()}")
                        # If download fails for the identified latest, stop for now.
                        return None
            else:
                # File not found remotely for this target_date, or other wget error
                logging.warning(f"Remote file not found for {date_str_compact}.")
                logging.warning(f"      wget --spider exit code: {spider_result.returncode}")
                # if spider_result.stderr:
                    # logging.warning(f"      STDERR: {spider_result.stderr.strip()}")
                if "ERROR 401" in spider_result.stderr or "Authentication R" in spider_result.stderr:
                    logging.error("CRITICAL: Authentication error with wget. Ensure .urs_cookies is valid, accessible, and not expired.")
                    return None # Stop if authentication fails
                # Continue to check the previous day
        
        except FileNotFoundError:
            logging.error("CRITICAL: `wget` command not found. Please ensure wget is installed and in your system's PATH.")
            return None
        except subprocess.TimeoutExpired:
            logging.error(f"CRITICAL: `wget` command timed out for {url}.")
            # Continue to check the previous day or retry, depending on strategy. For now, try older.
        except Exception as e:
            logging.error(f"CRITICAL: An unexpected error occurred during processing for {date_str_compact}: {e}")
            # Depending on the error, you might want to stop or try an older date.
            # For now, let's stop if an unexpected exception occurs for a date.
            return None

    logging.info(f"INFO: No new IMERG data found for the last {max_days_to_check} days.")
    return None

if __name__ == '__main__':
    logging.info("Starting IMERG data download process...")
    # Define base_dir for testing if __file__ is not available (e.g. running selection in IDE)
    # This assumes your CWD is the project root when testing this block directly.
    # If you run `python utils/imerg_downloader.py`, then __file__ will be set.
    
    # Example of setting base_dir manually if needed for testing:
    # test_base_dir = Path.cwd() # Or Path("/path/to/your/project_root")
    # print(f"Running test with base_dir: {test_base_dir}")
    
    downloaded_date = get_data()

    if downloaded_date:
        logging.info(f"New data downloaded for date: {downloaded_date}")
        # Here, your 'imerg_all' script would typically proceed to run the
        # indian_monsoon_script.py, potentially using this downloaded_date
        # to inform its processing or output directory naming.
        # For example:
        # print(f"Now, you would typically run the analysis script for {downloaded_date}.")
        # subprocess.run(["python", "utils/indian_monsoon_script.py"], check=True) # Adjust path as needed
    else:
        logging.info("No new data downloaded. Exiting process.")
