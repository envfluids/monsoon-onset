import os
from pathlib import Path
from glob import glob
import socket
from datetime import datetime
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
)
CLUSTER = "midway"

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
        if not isinstance(date_str, str) or len(date_str) != 8 or not date_str.isdigit():
            raise ValueError(
                f"Invalid date format: '{date_str}'. Expected 'YYYYMMDD' numeric string."
            )

        if date_str > most_recent:
            most_recent = date_str

    return most_recent


def sync_IMERG():
    base = Path(__file__).resolve().parent.parent.parent
    IMERG_output = base / "IMERG" / "output"
    try:
        date_dirs = glob(str(IMERG_output / "*"))
        date = find_most_recent_date([d.split("/")[-1] for d in date_dirs])
    except IndexError as e:
        logging.error(f"Failed to find latest directory: {e}")
        logging.info("Exiting sync process.")
        return
    drive_log = base / "sync" / "logs" / "IMERG_drive.txt"
    if not os.path.exists(drive_log):
        logging.info(f"Creating drive sync reference file at {drive_log}")
        with open(drive_log, "w") as f:
            f.write("")  # Placeholder for the first run
    else:
        with open(drive_log, "r") as f:
            drive_dates = f.read()
            dates_list = drive_dates.split("\n")
            if date in dates_list:
                logging.info(f"Date {date} already exists in drive sync reference file.")
                return
            else:
                logging.info(f"Date {date} does not exist in drive sync reference file.")
                try:
                    from drive import drive_sync_IMERG
                    drive_sync_IMERG(date, CLUSTER)
                    logging.info("IMERG data synced successfully.")
                    logging.info(f"Adding date {date} to drive sync reference file.")
                    with open(drive_log, "a") as f:
                        f.write(date + "\n")
                except Exception as e:
                    logging.error(f"Failed to sync with Google Drive: {e}")
                    return


def main():
    base = Path(__file__).resolve().parent.parent.parent
    operational_dir = base.parent / "monsoon-operational"
    live_dir = operational_dir / "docs" / "assets"
    maps_dir = live_dir / "images"
    data_dir = live_dir / "data"

    latest = base / "sync" / "latest"
    try:
        latest_dir = glob(str(latest / "*"))[0]
        date = latest_dir.split("/")[-1]
    except IndexError as e:
        logging.error(f"Failed to find latest directory: {e}")
        logging.info("Exiting sync process.")
        return

    live_date_ref = data_dir / "latest.txt"
    if not os.path.exists(live_date_ref):
        logging.info(f"Creating live date reference file at {live_date_ref}")
        with open(live_date_ref, "w") as f:
            f.write("XXXXXXXXTXX")  # Placeholder for the first run

    command = f"cd {operational_dir} && git pull"
    try:
        result = os.system(command)
        if result != 0:
            raise RuntimeError(f"Command failed with exit code {result}: {command}")
        logging.info(f"Pulled latest changes from operational repo.")
    except Exception as e:
        logging.error(f"Failed to pull changes from operational repo: {e}")
        return

    with open(live_date_ref, "r") as f:
        live_date = f.read().strip()

    if is_more_recent(live_date, date):
        logging.info(
            f"Latest forecast {date} is more recent that live date {live_date}. Updating live date."
        )

        command = f"rm -r {maps_dir}/*"
        os.system(command)
        command = f"rm -r {data_dir}/*"
        os.system(command)

        latest_maps = latest_dir + "/maps" + "/map_bars_with_probs_country_*.png"
        command = f"cp {latest_maps} {maps_dir}"
        os.system(command)

        # latest_data = latest_dir + "/blend_output_summary.csv"
        # command = f"cp {latest_data} {data_dir}"
        # os.system(command)

        current_name = glob(str(maps_dir / "*"))[0]
        remove_str = "_" + current_name.split("/")[-1].split("_")[-1].split(".")[0]
        new_name = current_name.replace(remove_str, "")
        command = f"mv {current_name} {new_name}"
        os.system(command)
        logging.info(f"Renamed {current_name} file to {new_name}")

        latest_messages = latest_dir + "/messages" + "/message_templates_output_eng.csv"
        command = f"cp {latest_messages} {data_dir}"
        os.system(command)

        with open(data_dir / "latest.txt", "w") as f:
            f.write(date)

        with open(data_dir / "cluster.txt", "w") as f:
            f.write("A")
            # f.write(socket.gethostname())

        logging.info(f"Updated live date to {date}.")

        command = f"cd {operational_dir} && git add . && git commit -m 'Updated live date to {date}' && git push"
        try:
            result = os.system(command)
            if result != 0:
                raise RuntimeError(f"Command failed with exit code {result}: {command}")
            logging.info(f"Pushed changes to operational repo.")
        except Exception as e:
            logging.error(f"Failed to push changes to operational repo: {e}")
    elif is_more_recent(date, live_date):
        logging.info(
            f"Latest forecast {date} is older than the live date {live_date}. No need to update."
        )
    else:
        logging.info(
            f"Latest forecast {date} is the same as the live date {live_date}. No need to update."
        )

    drive_log = base / "sync" / "logs" / "drive.txt"
    if not os.path.exists(drive_log):
        logging.info(f"Creating drive sync reference file at {drive_log}")
        with open(drive_log, "w") as f:
            f.write("")  # Placeholder for the first run
    else:
        with open(drive_log, "r") as f:
            drive_dates = f.read()
            dates_list = drive_dates.split("\n")
            if date in dates_list:
                logging.info(f"Date {date} already exists in drive sync reference file.")
            else:
                logging.info(f"Date {date} does not exist in drive sync reference file.")
                try:
                    from drive import drive_sync
                    drive_sync(date, CLUSTER)
                    logging.info(f"Adding date {date} to drive sync reference file.")
                    with open(drive_log, "a") as f:
                        f.write(date + "\n")
                except Exception as e:
                    logging.error(f"Failed to sync with Google Drive: {e}")
    
    # Sync IMERG data
    try:
        logging.info("Syncing IMERG data...")
        sync_IMERG()
    except Exception as e:
        logging.error(f"Failed to sync IMERG data: {e}")
        return
    logging.info(f"Sync process completed successfully.")


if __name__ == "__main__":
    main()
