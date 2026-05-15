"""
Monsoon Downloader — GCS Shim Wrapper

Downloads ECMWF or NCEP initial conditions using the original science scripts
(unmodified) and uploads the results to the COMMON bucket (shared across regions).

The science scripts use relative paths from their utils/ directory, so this
wrapper sets up the expected directory structure and cwd before invoking them.

Environment Variables:
    SOURCE             : 'ecmwf' | 'ncep' | 'both' for get_latest_date
    ACTION             : 'download' | 'get_latest_date'  (default: download)
    DATE               : Forecast date YYYYMMDDTHH (NeuralGCM date; ECMWF date is DATE-12h)
    GCS_COMMON_BUCKET  : Common bucket for ICs, GenCast SST, intermediate markers
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import requests
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

AIFS_UTILS    = Path("/app/AIFS/utils")
NGCM_UTILS    = Path("/app/NeuralGCM/utils")
GENCAST_UTILS = Path("/app/gencast/utils")
ECMWF_OPEN_DATA_BUCKET = "ecmwf-open-data"

# Sparse transform matrix filename used by download_ic.py
SPARSE_FILENAME = "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


_PUBLIC_CLIENT = None


def _public_client():
    global _PUBLIC_CLIENT
    if _PUBLIC_CLIENT is None:
        _PUBLIC_CLIENT = storage.Client.create_anonymous_client()
    return _PUBLIC_CLIENT


def blob_exists(bucket_name: str, gcs_path: str) -> bool:
    return _client().bucket(bucket_name).blob(gcs_path).exists()


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info(f"Uploaded {local_path} → gs://{bucket_name}/{gcs_path}")


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}: '{content}'")


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def download_public_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_name(local_path.name + ".tmp")
    blob = _public_client().bucket(bucket_name).blob(gcs_path)
    try:
        blob.download_to_filename(str(tmp_path))
        tmp_path.replace(local_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source", envvar="SOURCE", required=True,
              type=click.Choice(["ecmwf", "ncep", "both"]))
@click.option("--action", envvar="ACTION", default="download",
              type=click.Choice(["download", "get_latest_date"]))
@click.option("--date", envvar="DATE", default=None)
@click.option("--bucket", envvar="GCS_COMMON_BUCKET", required=True)
def main(source, action, date, bucket):
    if action == "get_latest_date":
        _get_latest_date(source, bucket)
    else:
        _download(source, date, bucket)


# ---------------------------------------------------------------------------
# get_latest_date: probe the data source without downloading the full file
# ---------------------------------------------------------------------------

def _get_latest_date(source: str, bucket: str) -> None:
    if source == "ecmwf":
        date_str = _latest_ecmwf_00z()
    elif source == "ncep":
        date_str = _latest_ncep_00z()
        if not date_str:
            logger.warning("No NCEP 00z data available — writing empty latest_date.txt")
            write_gcs_text(bucket, "intermediate/latest_date.txt", "")
            return
    else:
        ecmwf_date = _latest_ecmwf_00z()
        ncep_date = _latest_ncep_00z()
        if not ncep_date:
            logger.warning("No NCEP 00z data available — writing empty latest_date.txt")
            write_gcs_text(bucket, "intermediate/latest_date.txt", "")
            return
        date_str = min(ecmwf_date, ncep_date)
        logger.info(
            "Latest common 00z date: %s (ECMWF=%s, NCEP=%s)",
            date_str, ecmwf_date, ncep_date,
        )

    write_gcs_text(bucket, "intermediate/latest_date.txt", date_str)
    logger.info(f"Latest {source} date: {date_str}")


def _latest_ecmwf_00z() -> str:
    """Return the latest ECMWF 00z cycle at or before ECMWF open-data latest."""
    from ecmwf.opendata import Client as OpendataClient

    latest = OpendataClient(source="google").latest()
    latest_00z = latest.replace(hour=0, minute=0, second=0, microsecond=0)
    return latest_00z.strftime("%Y%m%dT%H")


def _latest_ncep_00z(max_days_back: int = 7) -> str | None:
    """Return the latest available NCEP/GDAS 00z cycle using HEAD requests only."""
    sys.path.insert(0, str(NGCM_UTILS))
    os.chdir(NGCM_UTILS)
    import download_ncep

    today_00z = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(max_days_back):
        candidate = today_00z - timedelta(days=days_back)
        date_str = candidate.strftime("%Y%m%dT%H")
        url, _, _ = download_ncep.get_cycle_for_date(download_ncep.BASE_URL_PATTERN, date_str)
        logger.info("Checking for NCEP 00z cycle: %s", url)
        try:
            response = requests.head(url, timeout=download_ncep.REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Error checking %s: %s", url, exc)
            continue
        if response.status_code == 200:
            return date_str
        if response.status_code != 404:
            logger.warning("Received status code %s for %s", response.status_code, url)
    return None


# ---------------------------------------------------------------------------
# download: fetch the IC file and push to GCS
# ---------------------------------------------------------------------------

def _download(source: str, date: str, bucket: str) -> None:
    if not date:
        date = _latest_ecmwf_00z() if source == "ecmwf" else _latest_ncep_00z()
    _require_00z(date)

    if source == "ecmwf":
        _download_ecmwf(date, bucket)
    elif source == "ncep":
        _download_ncep(date, bucket)
    else:
        raise click.ClickException("SOURCE=both is only valid with ACTION=get_latest_date")


def _require_00z(date: str) -> None:
    if not date or not date.endswith("T00"):
        raise click.ClickException(f"Only 00z initial conditions are supported; got DATE={date!r}")


def _download_ecmwf(date: str, bucket: str) -> None:
    """Run download_ic.get_data() and upload ECMWF IC + GenCast SST artifacts to GCS."""
    grib_prefix = f"ic/ecmwf/{date}/grib"
    expected_sst = f"ic/gencast_sst/{date}/sst_{date}.nc"
    if _ecmwf_gribs_exist(bucket, grib_prefix, date) and blob_exists(bucket, expected_sst):
        logger.info("ECMWF GRIB and GenCast SST artifacts already exist for %s; skipping download.", date)
        write_gcs_text(bucket, "intermediate/latest_ecmwf_date.txt", date)
        return

    (AIFS_UTILS.parent / "raw" / "ifs_ic" / "grib").mkdir(parents=True, exist_ok=True)
    sparse_local = AIFS_UTILS.parent / "EKR" / "mir_16_linear" / SPARSE_FILENAME
    sparse_local.parent.mkdir(parents=True, exist_ok=True)

    # Sparse transform matrix lives in the common bucket under weights/
    download_gcs_file(
        bucket,
        f"weights/aifs/EKR/mir_16_linear/{SPARSE_FILENAME}",
        sparse_local,
    )

    if _download_ecmwf_gribs_from_public_gcs(date):
        date_str = date
    else:
        logger.warning(
            "Falling back to AIFS download_ic.get_data() for ECMWF GRIBs for %s",
            date,
        )
        sys.path.insert(0, str(AIFS_UTILS))
        os.chdir(AIFS_UTILS)
        import download_ic

        date_str = download_ic.get_data(date)

        if not date_str:
            date_str = date
            logger.warning("ECMWF download_ic.get_data() returned None; checking local GRIB cache for %s", date_str)

    _upload_ecmwf_gribs(bucket, date_str)
    _upload_ecmwf_pickle_if_present(bucket, date_str)
    _download_and_upload_gencast_sst(bucket, date_str)

    write_gcs_text(bucket, "intermediate/latest_ecmwf_date.txt", date_str)


def _expected_ecmwf_grib_names(date_str: str) -> list[str]:
    date = datetime.strptime(date_str, "%Y%m%dT%H")
    dates = [date - timedelta(hours=12), date - timedelta(hours=6), date]
    return [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]


def _ecmwf_open_data_path(filename: str) -> str:
    ymd = filename[:8]
    hh = filename[8:10]
    return f"{ymd}/{hh}z/ifs/0p25/oper/{filename}"


def _local_ecmwf_gribs_exist(date_str: str) -> bool:
    grib_dir = AIFS_UTILS.parent / "raw" / "ifs_ic" / "grib"
    return all((grib_dir / filename).exists() for filename in _expected_ecmwf_grib_names(date_str))


def _download_ecmwf_gribs_from_public_gcs(date_str: str) -> bool:
    grib_dir = AIFS_UTILS.parent / "raw" / "ifs_ic" / "grib"
    grib_dir.mkdir(parents=True, exist_ok=True)

    if _local_ecmwf_gribs_exist(date_str):
        logger.info("All expected ECMWF GRIBs already exist locally for %s.", date_str)
        return True

    try:
        for filename in _expected_ecmwf_grib_names(date_str):
            local_path = grib_dir / filename
            if local_path.exists():
                logger.info("ECMWF GRIB %s already exists locally; skipping.", local_path)
                continue
            download_public_gcs_file(
                ECMWF_OPEN_DATA_BUCKET,
                _ecmwf_open_data_path(filename),
                local_path,
            )
    except Exception as exc:
        logger.warning(
            "Could not download ECMWF GRIBs for %s directly from gs://%s: %s",
            date_str,
            ECMWF_OPEN_DATA_BUCKET,
            exc,
        )

    return _local_ecmwf_gribs_exist(date_str)


def _ecmwf_gribs_exist(bucket: str, grib_prefix: str, date_str: str) -> bool:
    return all(
        blob_exists(bucket, f"{grib_prefix}/{filename}")
        for filename in _expected_ecmwf_grib_names(date_str)
    )


def _upload_ecmwf_gribs(bucket: str, date_str: str) -> None:
    grib_dir = AIFS_UTILS.parent / "raw" / "ifs_ic" / "grib"
    missing = []
    for filename in _expected_ecmwf_grib_names(date_str):
        local_path = grib_dir / filename
        if not local_path.exists():
            missing.append(str(local_path))
            continue
        upload_file(bucket, local_path, f"ic/ecmwf/{date_str}/grib/{filename}")
    if missing:
        raise RuntimeError("Missing expected ECMWF GRIB files: " + ", ".join(missing))


def _upload_ecmwf_pickle_if_present(bucket: str, date_str: str) -> None:
    local_pkl = AIFS_UTILS.parent / "raw" / "ifs_ic" / f"input_state_{date_str}.pkl"
    if not local_pkl.exists():
        logger.info("No ECMWF pickle found at %s; skipping pickle upload.", local_pkl)
        return
    upload_file(bucket, local_pkl, f"ic/ecmwf/{date_str}/input_state_{date_str}.pkl")


def _download_and_upload_gencast_sst(bucket: str, date_str: str) -> None:
    (GENCAST_UTILS.parent / "raw" / "sst_ic").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(GENCAST_UTILS))
    os.chdir(GENCAST_UTILS)
    import download_sst

    download_sst.get_sst(date_str)
    local_sst = GENCAST_UTILS.parent / "raw" / "sst_ic" / f"sst_{date_str}.nc"
    if not local_sst.exists():
        raise RuntimeError(f"GenCast SST download did not create {local_sst}")
    upload_file(bucket, local_sst, f"ic/gencast_sst/{date_str}/sst_{date_str}.nc")


def _download_ncep(date: str, bucket: str) -> None:
    """Run download_ncep.get_data() and upload the GRIB2 file to GCS."""
    gcs_path = f"ic/ncep/{date}/gdas_{date}.pgrb2"
    if blob_exists(bucket, gcs_path):
        logger.info("NCEP IC already exists at gs://%s/%s; skipping download.", bucket, gcs_path)
        write_gcs_text(bucket, "intermediate/latest_ncep_date.txt", date)
        return

    (NGCM_UTILS.parent / "raw" / "ncep_ic" / "download").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(NGCM_UTILS))
    os.chdir(NGCM_UTILS)
    import download_ncep

    date_str = download_ncep.get_data(date)

    if not date_str:
        raise RuntimeError("NCEP download_ncep.get_data() returned None — download failed")

    local_pgrb2 = NGCM_UTILS.parent / "raw" / "ncep_ic" / "download" / f"gdas_{date_str}.pgrb2"
    upload_file(bucket, local_pgrb2, f"ic/ncep/{date_str}/gdas_{date_str}.pgrb2")
    write_gcs_text(bucket, "intermediate/latest_ncep_date.txt", date_str)


if __name__ == "__main__":
    main()
