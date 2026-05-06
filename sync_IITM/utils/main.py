from pathlib import Path
from datetime import datetime
import logging
import json

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)


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


def valid_date_or_none(date_str):
    try:
        parse_date(date_str)
    except ValueError:
        return None
    return date_str


def get_local_tp_dates(tp_dir):
    dates = []

    for nc_file in sorted(tp_dir.glob("tp_*.nc")):
        date = nc_file.stem.replace("tp_", "", 1)
        if valid_date_or_none(date):
            dates.append(date)
        else:
            logging.warning(
                f"Skipping file with invalid date in name: {nc_file.name}"
            )

    return sorted(set(dates), key=parse_date)


def read_drive_dates(drive_log):
    if not drive_log.exists():
        logging.info(f"Creating drive sync reference file at {drive_log}")
        drive_log.parent.mkdir(parents=True, exist_ok=True)
        drive_log.write_text("")
        return []

    valid_dates = []
    with open(drive_log, "r") as f:
        for line in f:
            date = line.strip()
            if not date:
                continue
            if valid_date_or_none(date):
                valid_dates.append(date)
            else:
                logging.warning(
                    f"Skipping invalid date in drive sync reference file: {date}"
                )

    return valid_dates


def dates_to_sync(local_dates, drive_dates):
    drive_date_set = set(drive_dates)
    latest_drive_date = max(drive_dates, key=parse_date) if drive_dates else None

    sync_dates = []
    for date in local_dates:
        if date in drive_date_set:
            continue
        if latest_drive_date and parse_date(date) <= parse_date(latest_drive_date):
            continue
        sync_dates.append(date)

    return sync_dates


def main():
    base = Path(__file__).resolve().parent.parent.parent

    config_file = base / ".config" / "config.json"
    with open(config_file, "r") as f:
        config = json.load(f)
    cluster = config["cluster"]
    logging.info(f"Cluster: {cluster}")
    cluster_id = config["cluster_id"]
    logging.info(f"Cluster ID: {cluster_id}")

    tp_dir = base / "AIFS" / "output" / "tp_0p25"
    local_dates = get_local_tp_dates(tp_dir)
    if not local_dates:
        logging.error("No valid tp_*.nc files found in tp_0p25 directory.")
        logging.info("Exiting sync process.")
        return

    drive_log = base / "sync_IITM" / "logs" / "drive.txt"
    drive_dates = read_drive_dates(drive_log)
    sync_dates = dates_to_sync(local_dates, drive_dates)

    if not sync_dates:
        logging.info("No new dates to sync.")
        logging.info("Sync process completed successfully.")
        return

    logging.info(f"Found {len(sync_dates)} date(s) to sync: {sync_dates}")

    from drive import drive_sync

    for date in sync_dates:
        logging.info(f"Syncing missing date {date}.")
        try:
            sync_success = drive_sync(date, cluster)
        except Exception as e:
            logging.error(f"Failed to sync date {date} with Google Drive: {e}")
            logging.info("Stopping sync process to preserve chronological order.")
            return

        if not sync_success:
            logging.error(f"Drive sync did not complete successfully for date {date}.")
            logging.info("Stopping sync process to preserve chronological order.")
            return

        logging.info(f"Adding date {date} to drive sync reference file.")
        with open(drive_log, "a") as f:
            f.write(date + "\n")

    logging.info("Sync process completed successfully.")


if __name__ == "__main__":
    main()
