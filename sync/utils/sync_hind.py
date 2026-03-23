import os
from pathlib import Path
from glob import glob
import socket
from datetime import datetime
import logging
import json
from drive import drive_sync
import argparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s")



def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date", type=str, help="Dates for the upload in YYYYMMDDTHH format", nargs="+"
    )
    args = parser.parse_args()
    sync_dates = args.date

    logging.info("Initiating Hind Sync process")

    base = Path(__file__).resolve().parent.parent.parent
    config_file = base / ".config" / "config.json"
    with open(config_file, "r") as f:
        config = json.load(f)
    cluster = config["cluster"]
    logging.info(f"Cluster: {cluster}")

    logging.info(f"Syncing for dates: {sync_dates}, cluster: {cluster}")
    
    drive_log = base / "sync" / "logs" / "drive_hind.txt"
    if not os.path.exists(drive_log):
        logging.info(f"Creating drive sync reference file at {drive_log}")
        with open(drive_log, "w") as f:
            f.write("")  # Placeholder for the first run

    for sync_date in sync_dates:
        with open(drive_log, "r") as f:
            drive_dates = f.read()
            dates_list = drive_dates.split("\n")
            if sync_date in dates_list:
                logging.info(
                    f"Date {sync_date} already exists in drive sync reference file."
                )
            else:
                logging.info(
                    f"Date {sync_date} does not exist in drive sync reference file."
                )
                try:
                    drive_sync(sync_date, cluster)
                    logging.info(f"Adding date {sync_date} to drive sync reference file.")
                    with open(drive_log, "a") as f:
                        f.write(sync_date + "\n")
                except Exception as e:
                    logging.error(f"Failed to sync with Google Drive: {e}")

        logging.info("Hind Sync process completed successfully.")


if __name__ == "__main__":
    main()
