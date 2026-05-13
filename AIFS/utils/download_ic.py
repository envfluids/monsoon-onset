import argparse
import datetime
import logging
from pathlib import Path

import requests
from ecmwf.opendata import Client as OpendataClient
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

BASE_URL = "https://data.ecmwf.int/forecasts/"

DATE_SOURCE = "azure"
STREAMS = ["oper"]

BASE = Path(__file__).resolve().parent.parent

FINAL_OUTPUT_DIR = BASE / "raw" / "ifs_ic" / "AIFS"
GRIB_OUTPUT_DIR = BASE / "raw" / "ifs_ic" / "grib"

FINAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
GRIB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def check_new_data(date_str=None):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    logging.info(f"Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")

    if date_str:
        logging.info(f"Using provided date: {date_str}")
        date = datetime.datetime.strptime(date_str, "%Y%m%dT%H")
    else:
        logging.info(f"Fetching latest available date from {DATE_SOURCE}...")
        try:
            date = OpendataClient(source=DATE_SOURCE).latest()
        except Exception as e:
            logging.error(f"Error fetching latest date from {DATE_SOURCE}: {e}")
            logging.warning("Falling back to ECMWF for latest date...")
            try:
                date = OpendataClient().latest()
            except Exception as e:
                logging.error(f"Error fetching latest date from ECMWF: {e}")
                logging.error("Unable to fetch latest date. Exiting.")
                raise RuntimeError("Unable to fetch latest date from both sources.")

        date = date - datetime.timedelta(hours=date.hour)
        logging.info(f"Latest available date: {date.strftime('%Y-%m-%d %H:%M:%S')}")

        return date


def get_url(date, stream):
    ymd = date.strftime("%Y%m%d")
    hh = date.strftime("%H")
    return f"{BASE_URL}{ymd}/{hh}z/ifs/0p25/{stream}/{ymd}{hh}0000-0h-{stream}-fc.grib2"


def download_file(url, out_dir=GRIB_OUTPUT_DIR):
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = url.rsplit("/", 1)[-1]
    out_path = out_dir / filename
    tmp_path = out_path.with_name(out_path.name + ".tmp")

    if out_path.exists():
        logging.info(f"File {out_path} already exists. Skipping download.")
        return None

    logging.info(f"Downloading: {url}")

    try:
        with requests.get(url, stream=True) as response:
            response.raise_for_status()

            total = int(response.headers.get("content-length", 0))
            chunk_size = 1024 * 1024  # 1 MB

            with (
                open(tmp_path, "wb") as f,
                tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=f"Downloading {filename}",
                ) as progress,
            ):
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:  # skip keep-alive chunks
                        f.write(chunk)
                        progress.update(len(chunk))

        tmp_path.replace(out_path)
        logging.info(f"Data saved to {out_path}")
        return out_path

    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def get_data(date_str=None):
    date = check_new_data(date_str)
    downloaded_files = []
    if date:
        date_prev = date - datetime.timedelta(hours=6)
        for s in STREAMS:
            for d in [date_prev, date]:
                download_status = download_file(get_url(d, s))
                if download_status:
                    downloaded_files.append(download_status)

    if downloaded_files:
        date_formatted = date.strftime("%Y%m%dT%H")
        logging.info(f"New data downloaded successfully for {date_formatted}.")
        return date_formatted
    else:
        logging.info("No new data to download.")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Download initial conditions for IFS model"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date to download in format YYYYMMDDTHH. Defaults to latest.",
    )
    args = parser.parse_args()

    date_str = args.date

    get_data(date_str)


if __name__ == "__main__":
    main()
