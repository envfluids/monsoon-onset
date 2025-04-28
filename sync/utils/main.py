import os
from pathlib import Path
from glob import glob
import logging
import socket
from datetime import datetime


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


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
    )

    base = Path(__file__).resolve().parent.parent.parent
    operational_dir = base.parent / "monsoon-operational"
    live_dir = operational_dir / "docs" / "assets"
    maps_dir = live_dir / "images"
    data_dir = live_dir / "data"

    latest = base / "sync" / "latest"
    latest_dir = glob(str(latest / "*"))[0]
    date = latest_dir.split("/")[-1]

    live_date_ref = data_dir / "latest.txt"
    if not os.path.exists(live_date_ref):
        logging.info(f"Creating live date reference file at {live_date_ref}")
        with open(live_date_ref, "w") as f:
            f.write("XXXXXXXXT00")  # Placeholder for the first run

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

        latest_maps = latest_dir + "/maps" + "/map_bars.png"
        command = f"cp {latest_maps} {maps_dir}"
        os.system(command)

        latest_data = latest_dir + "/blend_output_summary.csv"
        command = f"cp {latest_data} {data_dir}"
        os.system(command)

        with open(data_dir / "latest.txt", "w") as f:
            f.write(date)

        with open(data_dir / "cluster.txt", "w") as f:
            f.write(socket.gethostname())

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

    logging.info("Sync process completed.")


if __name__ == "__main__":
    main()
