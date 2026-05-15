#!/usr/bin/env python3
"""Download a single NCMRWF precipitation forecast file."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from pathlib import Path

import requests
from tqdm import tqdm
from preprocess_ncum import preprocess_ncum


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

DATE_RE = re.compile(r"^\d{8}T00$")
DEFAULT_SERVER = "https://cloud.ncmrwf.gov.in"
REQUEST_TIMEOUT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 15
CHUNK_SIZE_BYTES = 1024 * 1024
DEFAULT_LOOKBACK_DAYS = 7


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent

def load_auth(auth_file: Path) -> tuple[str, str]:
    if not auth_file.exists():
        raise FileNotFoundError(f"Auth file not found: {auth_file}")
    if not auth_file.is_file():
        raise FileNotFoundError(f"Auth path is not a file: {auth_file}")

    try:
        auth = json.loads(auth_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Auth file is not valid JSON: {auth_file}") from exc

    if not isinstance(auth, dict):
        raise ValueError(f"Auth file must contain a JSON object: {auth_file}")

    api_key = auth.get("API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        raise KeyError(f"Missing API_KEY in auth file: {auth_file}")

    server = auth.get("SERVER", DEFAULT_SERVER)
    if not isinstance(server, str) or not server.strip():
        raise KeyError(f"SERVER in auth file must be a non-empty string: {auth_file}")

    return api_key.strip(), server.strip()


def output_path_for(date: str) -> Path:
    return (
        project_root()
        / "raw"
        / "precipitation_amount"
        / f"precipitation_amount_{date}.nc"
    )


def processed_output_path_for(date: str) -> Path:
    return (
        project_root()
        / "output"
        / "precipitation_amount"
        / f"precipitation_amount_{date}.nc"
    )


def validate_date(date: str) -> None:
    if not DATE_RE.fullmatch(date):
        raise ValueError(
            f"Date must use YYYYMMDDT00 format, for example 20260512T00: {date}"
        )


def candidate_dates(max_days_back: int = DEFAULT_LOOKBACK_DAYS) -> list[str]:
    max_days_back = max(1, max_days_back)
    today_00z = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return [
        (today_00z - timedelta(days=days_back)).strftime("%Y%m%dT00")
        for days_back in range(max_days_back)
    ]


def remote_file_for(date: str) -> str:
    run_date = date.removesuffix("T00")
    return f"{date[:4]}/{run_date}.nc"


def download_request(date: str, api_key: str, server: str) -> tuple[str, dict[str, str]]:
    params = {
        "action": "download",
        "file": remote_file_for(date),
        "api_key": api_key,
    }
    url = f"{server.rstrip('/')}/file_manager.php"
    return url, params


def response_looks_downloadable(response: requests.Response) -> bool:
    if response.status_code != 200:
        return False

    content_type = response.headers.get("content-type", "").lower()
    if any(
        marker in content_type for marker in ("text/html", "application/json", "xml")
    ):
        return False

    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) <= 0:
                return False
        except ValueError:
            logging.warning("Unexpected content-length header: %s", content_length)

    return True


def remote_file_available(date: str, api_key: str, server: str) -> bool:
    url, params = download_request(date, api_key, server)
    remote_file = params["file"]

    logging.info("Checking for NCUM forecast: %s", remote_file)
    try:
        response = requests.head(
            url,
            params=params,
            allow_redirects=True,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logging.warning("HEAD check failed for %s: %s", remote_file, exc)
    else:
        if response_looks_downloadable(response):
            logging.info("Found available NCUM forecast: %s", remote_file)
            return True
        if response.status_code not in {405, 501}:
            logging.info(
                "NCUM forecast not available for %s: HTTP %s",
                remote_file,
                response.status_code,
            )
            return False

    try:
        with requests.get(
            url,
            params=params,
            stream=True,
            timeout=CHECK_TIMEOUT_SECONDS,
        ) as response:
            if response_looks_downloadable(response):
                logging.info("Found available NCUM forecast: %s", remote_file)
                return True
            logging.info(
                "NCUM forecast not available for %s: HTTP %s",
                remote_file,
                response.status_code,
            )
            return False
    except requests.RequestException as exc:
        logging.warning("GET check failed for %s: %s", remote_file, exc)
        return False


def latest_available_date(
    api_key: str, server: str, max_days_back: int = DEFAULT_LOOKBACK_DAYS
) -> str | None:
    for date in candidate_dates(max_days_back):
        if remote_file_available(date, api_key, server):
            return date

    logging.warning(
        "No NCUM forecast found after checking the last %s days.", max_days_back
    )
    return None


def file_is_complete(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def prepare_output_path(output_path: Path, overwrite: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.parent.is_dir():
        raise NotADirectoryError(f"Output directory is not valid: {output_path.parent}")

    if not output_path.exists():
        return

    size = output_path.stat().st_size
    if size > 0 and not overwrite:
        raise FileExistsError(f"Output file already exists: {output_path}")

    if size == 0:
        logging.warning("Removing empty output file: %s", output_path)
        output_path.unlink()


def download_forecast(date: str, api_key: str, server: str, output_path: Path) -> None:
    url, params = download_request(date, api_key, server)
    remote_file = params["file"]
    temp_path = output_path.with_name(f"{output_path.name}.part")

    if temp_path.exists():
        logging.warning("Removing stale partial download: %s", temp_path)
        temp_path.unlink()

    logging.info("Downloading %s", remote_file)
    logging.info("Saving to %s", output_path)

    try:
        with requests.get(
            url,
            params=params,
            stream=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            if not response_looks_downloadable(response):
                raise RuntimeError(
                    f"Remote response does not look like a NetCDF download: {remote_file}"
                )
            expected_size = int(response.headers.get("content-length", 0))

            with temp_path.open("wb") as file_obj, tqdm(
                desc=output_path.name,
                total=expected_size or None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
            ) as progress:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE_BYTES):
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    progress.update(len(chunk))

        actual_size = temp_path.stat().st_size
        if actual_size == 0:
            raise RuntimeError(f"Downloaded file is empty: {temp_path}")
        if expected_size and actual_size != expected_size:
            raise RuntimeError(
                f"Incomplete download: expected {expected_size} bytes, got {actual_size} bytes"
            )

        temp_path.replace(output_path)
        logging.info("Download complete: %s (%d bytes)", output_path, actual_size)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def ensure_processed(date: str) -> bool:
    processed_path = processed_output_path_for(date)
    if file_is_complete(processed_path):
        logging.info("Processed NCUM file already exists: %s", processed_path)
        return False

    logging.info("Preprocessing NCUM forecast for %s", date)
    preprocess_ncum(date, project_root())
    return True


def get_data(
    date: str | None = None,
    overwrite: bool = False,
    max_days_back: int = DEFAULT_LOOKBACK_DAYS,
) -> str | None:
    try:
        api_key, server = load_auth(project_root() / ".auth" / "keys.json")
        remote_checked = False

        if date is not None:
            validate_date(date)
        else:
            date = latest_available_date(api_key, server, max_days_back)
            if date is None:
                return None
            remote_checked = True

        output_path = output_path_for(date)
        if file_is_complete(output_path) and not overwrite:
            logging.info("Raw NCUM file already exists: %s", output_path)
            if ensure_processed(date):
                return date
            return None

        if not remote_checked and not remote_file_available(date, api_key, server):
            logging.info("NCUM forecast is not available: %s", date)
            return None

        prepare_output_path(output_path, overwrite)
        download_forecast(date, api_key, server, output_path)
        if ensure_processed(date):
            return date
        return date
    except Exception as exc:
        logging.error("Failed to download forecast: %s", exc)
        return None

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download one NCMRWF precipitation_amount NetCDF forecast file.",
    )
    parser.add_argument(
        "--date",
        help="Forecast cycle in YYYYMMDDT00 format, for example 20260512T00.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the output file if it already exists.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="Number of recent 00z forecast dates to check when --date is omitted.",
    )
    args = parser.parse_args()

    date = get_data(args.date, args.overwrite, args.lookback_days)
    if date:
        logging.info(f"Successfully downloaded and processed forecast for date: {date}")
    else:
        logging.info("No new forecast downloaded.")

if __name__ == "__main__":
    main()
