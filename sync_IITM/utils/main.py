import os
from pathlib import Path
from glob import glob
import socket
from datetime import datetime
import logging
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")


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


def is_more_recent(date_str1, date_str2):
    """
    Compares two date strings using parse_date and checks if date_str2 is more recent than date_str1.

    Args:
        date_str1 (str): The first date string in the format 'YYYYMMDDTHH'.
        date_str2 (str): The second date string in the format 'YYYYMMDDTHH'.

    Returns:
        bool: True if date_str2 is more recent than date_str1, False otherwise.
    """
    date1 = parse_date(date_str1)
    date2 = parse_date(date_str2)
    return date2 > date1


def find_most_recent_date(date_list):
    """
    Finds the most recent date from a list of date strings.

    The date strings are expected to be in 'YYYYMMDD' format.
    This format allows for direct string comparison to determine recency,
    as lexicographical order will correspond to chronological order.

    Args:
        date_list: A list of strings, where each string is a date
                   formatted as 'YYYYMMDD'. Example: ["20230115", "20230120"].

    Returns:
        A string representing the most recent date from the list.
        Returns None if the input list is empty or None.

    Raises:
        ValueError: If any date string in the list is not in the
                    expected 'YYYYMMDD' format or is not a valid date string.
    """
    if not date_list:
        return None  # Handle empty or None list

    most_recent = ""  # Initialize with an empty string

    for date_str in date_list:
        # Basic validation for format (8 digits)
        if (
            not isinstance(date_str, str)
            or len(date_str) != 8
            or not date_str.isdigit()
        ):
            raise ValueError(
                f"Invalid date format: '{date_str}'. Expected 'YYYYMMDD' numeric string."
            )

        if date_str > most_recent:
            most_recent = date_str

    return most_recent




def main():
    base = Path(__file__).resolve().parent.parent.parent
    operational_dir = base.parent / "monsoon-operational"
    live_dir = operational_dir / "docs" / "assets"
    maps_dir = live_dir / "images"
    data_dir = live_dir / "data"

    config_file = base / ".config" / "config.json"
    with open(config_file, "r") as f:
        config = json.load(f)
    cluster = config["cluster"]
    logging.info(f"Cluster: {cluster}")
    cluster_id = config["cluster_id"]
    logging.info(f"Cluster ID: {cluster_id}")

    tp_dir = base / "AIFS" / "output" / "tp_0p25"
    try:
        nc_files = sorted(glob(str(tp_dir / "tp_*.nc")))
        if not nc_files:
            logging.error("No .nc files found in tp_0p25 directory.")
            logging.info("Exiting sync process.")
            return
        latest_file = nc_files[-1]
        date = Path(latest_file).stem.replace("tp_", "")
        logging.info(f"Found latest file: {latest_file}, date: {date}")
    except Exception as e:
        logging.error(f"Failed to find latest nc file: {e}")
        logging.info("Exiting sync process.")
        return

    drive_log = base / "sync_IITM" / "logs" / "drive.txt"
    if not os.path.exists(drive_log):
        logging.info(f"Creating drive sync reference file at {drive_log}")
        with open(drive_log, "w") as f:
            f.write("")  # Placeholder for the first run
    else:
        with open(drive_log, "r") as f:
            drive_dates = f.read()
            dates_list = drive_dates.split("\n")
            if date in dates_list:
                logging.info(
                    f"Date {date} already exists in drive sync reference file."
                )
            else:
                logging.info(
                    f"Date {date} does not exist in drive sync reference file."
                )
                try:
                    from drive import drive_sync

                    drive_sync(date, cluster)
                    logging.info(f"Adding date {date} to drive sync reference file.")
                    with open(drive_log, "a") as f:
                        f.write(date + "\n")
                except Exception as e:
                    logging.error(f"Failed to sync with Google Drive: {e}")


    logging.info("Sync process completed successfully.")


if __name__ == "__main__":
    main()
