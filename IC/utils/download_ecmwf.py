import argparse
import datetime
import json
import logging
import time
from pathlib import Path

import requests
from ecmwf.opendata import Client as OpendataClient
import earthkit.data as ekd
from tqdm import tqdm
import numpy as np
import xarray as xr

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

ECMWF_BASE_URL = "https://data.ecmwf.int/forecasts/"
GOOGLE_BASE_URL = "https://storage.googleapis.com/ecmwf-open-data/"

DATE_SOURCE = "aws"
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

BASE = Path(__file__).resolve().parent.parent

REPO_ROOT = BASE.parent
CONFIG_DIR = REPO_ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "models.json"

GRIB_OUTPUT_DIR = BASE / "ecmwf"
GRIB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SST_DIR = BASE / "ecmwf"
SST_DIR.mkdir(parents=True, exist_ok=True)

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

def get_streams_deltas():
    streams_dict = {}

    download_mars = False
    for model, model_config in CONFIG.items():
        if model_config["ic_source"] == "ecmwf":
            streams = model_config["ic_streams"]
            for stream in streams:
                if stream not in streams_dict:
                    streams_dict[stream] = set([0])
                streams_dict[stream].add(model_config["ic_timedelta"])
        if model_config["PARAM_MARS"]:
            download_mars = True

    return streams_dict, download_mars


def get_mars_retrieval(date, param):

    request = {
        "class": "od",
        "date": date.strftime("%Y-%m-%d"),
        "expver": "1",
        "param": param,
        "step": 0,
        "stream": "oper",
        "time": date.strftime("%H:%M:%S"),
        "type": "fc",
        "grid": "0.25/0.25",
        "REPRES": "LL",
    }

    levtype = "sfc"
    request["levtype"] = levtype

    print(request)

    return request

def get_open_data(DATE, param):
    # Get the data for the current date and the previous date
    data_list = []
    for date in [DATE - datetime.timedelta(hours=12), DATE]:
        date_mars = date - datetime.timedelta(hours=24)
        data = ekd.from_source("mars", request=get_mars_retrieval(date_mars, param))
        data = data.to_xarray()
        if "time" not in data.coords:
            data = data.expand_dims("time")
            data["time"] = [np.datetime64(date)]
        if data.longitude.max().item() < 200:
            data = data.assign_coords(longitude=data.longitude % 360)
            data = data.sortby("longitude")

        data_list.append(data)

    ds = xr.concat(data_list, dim="time")

    return ds

def get_sst(date):
    SST_DIR.mkdir(parents=True, exist_ok=True)
    date_f = date.strftime("%Y%m%dT%H")
    out_path = SST_DIR / f"sst_{date_f}.nc"
    tmp_path = out_path.with_name(out_path.name + ".tmp")

    if out_path.exists():
        logging.info("SST file %s already exists. Skipping download.", out_path)
        return out_path
    param = CONFIG["gencast"]["PARAM_MARS"]
    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
    sfc_mars = get_open_data(DATE=date, param=list(param.keys()))
    sfc_mars = sfc_mars.rename(param)

    for var in sfc_mars.data_vars:
        sfc_mars[var] = sfc_mars[var].astype(np.float32)
        sfc_mars[var].attrs.pop("_earthkit", None)

    if tmp_path.exists():
        tmp_path.unlink()

    try:
        sfc_mars.to_netcdf(tmp_path)
        tmp_path.replace(out_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    if not out_path.exists():
        raise RuntimeError(f"SST download did not create {out_path}")

    logging.info("SST file saved to %s", out_path)
    return out_path

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
                raise RuntimeError("Unable to fetch latest date from any source.")

        date = date - datetime.timedelta(hours=date.hour)
        logging.info(f"Latest available date: {date.strftime('%Y-%m-%d %H:%M:%S')}")

    return date


def get_grib_filename(date, stream):
    ymd = date.strftime("%Y%m%d")
    hh = date.strftime("%H")
    return f"{ymd}{hh}0000-0h-{stream}-fc.grib2"


def get_url(date, stream, source="google"):
    ymd = date.strftime("%Y%m%d")
    hh = date.strftime("%H")
    filename = get_grib_filename(date, stream)
    path = f"{ymd}/{hh}z/ifs/0p25/{stream}/{filename}"
    if source == "google":
        return f"{GOOGLE_BASE_URL}{path}"
    if source == "ecmwf":
        return f"{ECMWF_BASE_URL}{path}"
    raise ValueError(f"Unknown ECMWF GRIB source: {source}")


def get_urls(date, stream):
    return [get_url(date, stream, source) for source in ("google", "ecmwf")]


def _cleanup_tmp(tmp_path):
    if tmp_path.exists():
        tmp_path.unlink()


def _sleep_before_retry(url, status_code, attempt, max_retries, backoff_factor):
    sleep_seconds = backoff_factor * (2**attempt)
    logging.warning(
        "Download failed for %s with HTTP %s. Retrying in %s seconds "
        "(attempt %s of %s).",
        url,
        status_code,
        sleep_seconds,
        attempt + 1,
        max_retries,
    )
    time.sleep(sleep_seconds)


def download_file(urls, out_dir=GRIB_OUTPUT_DIR, max_retries=25, backoff_factor=2):
    out_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(urls, str):
        urls = [urls]

    filename = urls[0].rsplit("/", 1)[-1]
    out_path = out_dir / filename
    tmp_path = out_path.with_name(out_path.name + ".tmp")

    if out_path.exists():
        logging.info(f"File {out_path} already exists. Skipping download.")
        return None

    last_error = None
    for url in urls:
        logging.info(f"Downloading: {url}")

        for attempt in range(max_retries + 1):
            try:
                with requests.get(url, stream=True, timeout=(10, 120)) as response:
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

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                _cleanup_tmp(tmp_path)
                last_error = e

                if (
                    status_code in RETRYABLE_HTTP_STATUS
                    and attempt < max_retries
                ):
                    _sleep_before_retry(
                        url, status_code, attempt, max_retries, backoff_factor
                    )
                    continue

                logging.warning(
                    "Download source failed for %s with HTTP %s; trying fallback if available.",
                    url,
                    status_code,
                )
                break

            except requests.exceptions.RequestException as e:
                _cleanup_tmp(tmp_path)
                last_error = e

                if attempt < max_retries:
                    _sleep_before_retry(
                        url, type(e).__name__, attempt, max_retries, backoff_factor
                    )
                    continue

                logging.warning(
                    "Download source failed for %s with %s; trying fallback if available.",
                    url,
                    type(e).__name__,
                )
                break

            except Exception:
                _cleanup_tmp(tmp_path)
                raise

    raise RuntimeError(f"Unable to download {filename} from any configured source") from last_error


def get_data(date_str=None):
    date = check_new_data(date_str)
    downloaded_files = []
    if date:
        streams_deltas = get_streams_deltas()
        for stream, delta in streams_deltas:
            for d in delta:
                download_date = date - datetime.timedelta(hours=d)
                download_status = download_file(get_urls(download_date, stream))
                if download_status:
                    downloaded_files.append(download_status)
        get_sst(date)

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
