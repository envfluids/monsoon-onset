"""
Monsoon Downloader — GCS Shim Wrapper

Downloads ECMWF or NCEP initial conditions using the original science scripts
(unmodified) and uploads the results to GCS.

The science scripts use relative paths from their utils/ directory, so this
wrapper sets up the expected directory structure and cwd before invoking them.

Environment Variables:
    SOURCE          : 'ecmwf' or 'ncep'
    ACTION          : 'download' | 'get_latest_date'  (default: download)
    DATE            : Forecast date YYYYMMDDTHH (NeuralGCM date; ECMWF date is DATE-12h)
    FORECAST_REGION : e.g. 'india'
    GCS_BUCKET      : Main data bucket
    GCS_WEIGHTS_BUCKET : Weights/static-files bucket
"""

import os
import sys
import logging
from pathlib import Path

import click
from google.cloud import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Paths matching the directory structure the science scripts expect
AIFS_UTILS  = Path("/app/AIFS/utils")
NGCM_UTILS  = Path("/app/NeuralGCM/utils")

# Sparse transform matrix filename (hardcoded in download_ic.py)
SPARSE_FILENAME = "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    blob = _client().bucket(bucket_name).blob(gcs_path)
    blob.upload_from_filename(str(local_path))
    logger.info(f"Uploaded {local_path} → gs://{bucket_name}/{gcs_path}")


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    blob = _client().bucket(bucket_name).blob(gcs_path)
    blob.upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}: '{content}'")


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob = _client().bucket(bucket_name).blob(gcs_path)
    blob.download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source",         envvar="SOURCE",            required=True,
              type=click.Choice(["ecmwf", "ncep"]))
@click.option("--action",         envvar="ACTION",            default="download",
              type=click.Choice(["download", "get_latest_date"]))
@click.option("--date",           envvar="DATE",              default=None)
@click.option("--region",         envvar="FORECAST_REGION",   default="india")
@click.option("--bucket",         envvar="GCS_BUCKET",        required=True)
@click.option("--weights-bucket", envvar="GCS_WEIGHTS_BUCKET", required=True)
def main(source, action, date, region, bucket, weights_bucket):
    if action == "get_latest_date":
        _get_latest_date(source, region, bucket)
    else:
        _download(source, date, region, bucket, weights_bucket)


# ---------------------------------------------------------------------------
# get_latest_date: probe the data source without downloading the full file
# ---------------------------------------------------------------------------

def _get_latest_date(source: str, region: str, bucket: str) -> None:
    if source == "ecmwf":
        # download_ic.check_new_data() pings the ECMWF API for the latest date
        sys.path.insert(0, str(AIFS_UTILS))
        os.chdir(AIFS_UTILS)
        import download_ic
        from ecmwf.opendata import Client as OpendataClient
        date_str = OpendataClient().latest().strftime("%Y%m%dT%H")
    else:
        # download_ncep exposes its internals so we can do a HEAD-only check
        sys.path.insert(0, str(NGCM_UTILS))
        os.chdir(NGCM_UTILS)
        import download_ncep
        url, filename, _ = download_ncep.get_latest_available_cycle(
            download_ncep.BASE_URL_PATTERN,
            download_ncep.CYCLES_TO_CHECK,
            download_ncep.CYCLE_HOURS,
            download_ncep.REQUEST_TIMEOUT,
        )
        if not url:
            logger.warning("No NCEP data available — writing empty latest_date.txt")
            write_gcs_text(bucket, f"{region}/intermediate/latest_date.txt", "")
            return
        # filename format: gdas_{YYYYMMDD}T{HH}.pgrb2
        date_str = filename.replace("gdas_", "").replace(".pgrb2", "")

    write_gcs_text(bucket, f"{region}/intermediate/latest_date.txt", date_str)
    logger.info(f"Latest {source} date: {date_str}")


# ---------------------------------------------------------------------------
# download: fetch the IC file and push to GCS
# ---------------------------------------------------------------------------

def _download(source: str, date: str, region: str, bucket: str, weights_bucket: str) -> None:
    if source == "ecmwf":
        _download_ecmwf(region, bucket, weights_bucket)
    else:
        _download_ncep(region, bucket)


def _download_ecmwf(region: str, bucket: str, weights_bucket: str) -> None:
    """Run download_ic.get_data() and upload the resulting pickle to GCS.

    The science script always fetches the latest available ECMWF cycle.
    It saves the IC as ../raw/ifs_ic/input_state_{date}.pkl relative to cwd,
    and also needs the sparse transform matrix at ../EKR/mir_16_linear/{hash}.npz.
    """
    # Create expected directory tree
    (AIFS_UTILS.parent / "raw" / "ifs_ic").mkdir(parents=True, exist_ok=True)
    sparse_local = AIFS_UTILS.parent / "EKR" / "mir_16_linear" / SPARSE_FILENAME
    sparse_local.parent.mkdir(parents=True, exist_ok=True)

    # Download sparse matrix from weights bucket
    download_gcs_file(
        weights_bucket,
        f"aifs/EKR/mir_16_linear/{SPARSE_FILENAME}",
        sparse_local,
    )

    sys.path.insert(0, str(AIFS_UTILS))
    os.chdir(AIFS_UTILS)
    import download_ic

    date_str = download_ic.get_data()

    if not date_str:
        # File may already exist locally (shouldn't happen in a fresh container, but handle it)
        ifs_dir = AIFS_UTILS.parent / "raw" / "ifs_ic"
        pkls = sorted(ifs_dir.glob("input_state_*.pkl"))
        if not pkls:
            raise RuntimeError("ECMWF download_ic.get_data() returned None and no local file found")
        date_str = pkls[-1].stem.replace("input_state_", "")
        logger.warning(f"Using existing local IC file for date: {date_str}")

    local_pkl = AIFS_UTILS.parent / "raw" / "ifs_ic" / f"input_state_{date_str}.pkl"
    upload_file(bucket, local_pkl, f"{region}/raw/ecmwf/{date_str}/input_state_{date_str}.pkl")

    # Record the actual ECMWF date so the AIFS container can look it up
    write_gcs_text(bucket, f"{region}/intermediate/latest_ecmwf_date.txt", date_str)


def _download_ncep(region: str, bucket: str) -> None:
    """Run download_ncep.get_data() and upload the GRIB2 file to GCS.

    The science script saves the file as ../raw/ncep_ic/download/gdas_{date}.pgrb2
    relative to cwd.
    """
    (NGCM_UTILS.parent / "raw" / "ncep_ic" / "download").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(NGCM_UTILS))
    os.chdir(NGCM_UTILS)
    import download_ncep

    date_str = download_ncep.get_data()

    if not date_str:
        raise RuntimeError("NCEP download_ncep.get_data() returned None — download failed")

    # filename: gdas_{YYYYMMDD}T{HH}.pgrb2  →  date_str = {YYYYMMDD}T{HH}
    local_pgrb2 = NGCM_UTILS.parent / "raw" / "ncep_ic" / "download" / f"gdas_{date_str}.pgrb2"
    upload_file(bucket, local_pgrb2, f"{region}/raw/ncep/{date_str}/gdas_{date_str}.pgrb2")


if __name__ == "__main__":
    main()
