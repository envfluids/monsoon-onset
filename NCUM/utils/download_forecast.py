#!/usr/bin/env python3
"""Download a single NCMRWF precipitation forecast file."""

from __future__ import annotations

import argparse
from datetime import datetime
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
CHUNK_SIZE_BYTES = 1024 * 1024


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
    return project_root() / "raw" / "precipitation_amount" / f"precipitation_amount_{date}.nc"


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
    run_date = date.removesuffix("T00")
    remote_file = f"{date[:4]}/{run_date}.nc"
    temp_path = output_path.with_name(f"{output_path.name}.part")

    if temp_path.exists():
        logging.warning("Removing stale partial download: %s", temp_path)
        temp_path.unlink()

    params = {
        "action": "download",
        "file": remote_file,
        "api_key": api_key,
    }
    url = f"{server.rstrip('/')}/file_manager.php"

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



def get_data(date: str | None = None, overwrite: bool = False) -> str | None:

    if date is None:
        date = datetime.now().strftime("%Y%m%dT00")
        logging.info(f"No date provided, using current date: {date}")
    try:
        api_key, server = load_auth(project_root() / ".auth" / "keys.json")
        output_path = output_path_for(date)
        if output_path.exists():
            logging.info("Output file already exists: %s", output_path)
            return None
        else:
            prepare_output_path(output_path, overwrite)
            download_forecast(date, api_key, server, output_path)
            preprocess_ncum(date, project_root())
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
    args = parser.parse_args()

    date = get_data(args.date, args.overwrite)
    if date:
        logging.info(f"Successfully downloaded and processed forecast for date: {date}")
    else:
        logging.info("No new forecast downloaded.")

if __name__ == "__main__":
    main()
