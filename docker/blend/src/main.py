"""
Monsoon Blend — GCS Shim Wrapper

Downloads AIFS and NeuralGCM TP outputs from GCS into the directory structure
that blend/utils/main.py expects, runs the blend pipeline using the original
unmodified science script, then uploads outputs to GCS.

blend/utils/main.py computes its `base` path as:
    Path(__file__).resolve().parent.parent.parent
so with the script at /app/blend/utils/main.py, base = /app.

Expected local paths (under /app):
    /app/AIFS/output/tp/tp_{aifs_date}.nc         ← AIFS precip output
    /app/NeuralGCM_google/output/tp/tp_{date}.nc  ← NeuralGCM precip output
    /app/blend/data/support/                       ← bundled in image
    /app/blend/data/support/large/                 ← climatology CSVs from weights bucket

Environment Variables:
    DATE               : NeuralGCM date YYYYMMDDTHH
    FORECAST_REGION    : e.g. 'india'
    GCS_BUCKET         : Main data bucket
    GCS_WEIGHTS_BUCKET : Weights/static-files bucket
"""

import os
import sys
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

BLEND_UTILS = Path("/app/blend/utils")


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob = _client().bucket(bucket_name).blob(gcs_path)
    blob.download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def download_gcs_prefix(bucket_name: str, gcs_prefix: str, local_dir: Path) -> None:
    """Download all blobs under gcs_prefix into local_dir."""
    client = _client()
    for blob in client.list_blobs(bucket_name, prefix=gcs_prefix):
        relative = blob.name[len(gcs_prefix):].lstrip("/")
        if not relative:
            continue
        local_path = local_dir / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
        logger.info(f"Downloaded gs://{bucket_name}/{blob.name} → {local_path}")


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    client = _client()
    bucket = client.bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info(f"Uploaded {local_file} → gs://{bucket_name}/{gcs_path}")


def read_gcs_text(bucket_name: str, gcs_path: str) -> str:
    return _client().bucket(bucket_name).blob(gcs_path).download_as_text().strip()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",           envvar="DATE",              required=True)
@click.option("--region",         envvar="FORECAST_REGION",   default="india")
@click.option("--bucket",         envvar="GCS_BUCKET",        required=True)
@click.option("--weights-bucket", envvar="GCS_WEIGHTS_BUCKET", required=True)
def main(date, region, bucket, weights_bucket):
    # Resolve the AIFS date (12h before NeuralGCM date, matching blend/utils/main.py logic)
    try:
        aifs_date = read_gcs_text(bucket, f"{region}/intermediate/latest_ecmwf_date.txt")
        logger.info(f"AIFS date from GCS: {aifs_date}")
    except Exception:
        aifs_date = (datetime.strptime(date, "%Y%m%dT%H") - timedelta(hours=12)).strftime("%Y%m%dT%H")
        logger.warning(f"Could not read latest_ecmwf_date.txt; using computed aifs_date={aifs_date}")

    _setup_directories(date, aifs_date)
    _download_inputs(date, aifs_date, region, bucket, weights_bucket)
    _run_science_script(date)
    _upload_outputs(date, region, bucket)


def _setup_directories(date: str, aifs_date: str) -> None:
    for d in [
        f"/app/AIFS/output/tp",
        f"/app/NeuralGCM_google/output/tp",
        f"/app/blend/output_google/{date}",
        f"/app/blend/data/support/large",
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)


def _download_inputs(date: str, aifs_date: str, region: str,
                     bucket: str, weights_bucket: str) -> None:
    # 1. AIFS precipitation output
    #    blend/utils/main.py computes AIFS_date = parse_date(date) - 12h and reads tp_{AIFS_date}.nc
    download_gcs_file(
        bucket,
        f"{region}/output/aifs/{aifs_date}/tp_{aifs_date}.nc",
        Path(f"/app/AIFS/output/tp/tp_{aifs_date}.nc"),
    )

    # 2. NeuralGCM precipitation output
    #    With --source google, blend reads from NeuralGCM_google/output/tp/tp_{date}.nc
    download_gcs_file(
        bucket,
        f"{region}/output/neuralgcm/{date}/tp_{date}.nc",
        Path(f"/app/NeuralGCM_google/output/tp/tp_{date}.nc"),
    )

    # 3. Blend climatology CSVs (large files not bundled in the image)
    download_gcs_prefix(
        weights_bucket,
        "blend/support/large/",
        Path("/app/blend/data/support/large"),
    )


def _run_science_script(date: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(BLEND_UTILS)}

    # blend/utils/main.py computes base = Path(__file__).parent.parent.parent = /app
    # With --source google it reads NeuralGCM_google/ outputs and writes to blend/output_google/
    logger.info(f"Running blend/utils/main.py for {date}")
    subprocess.run(
        [sys.executable, "main.py", "--date", date, "--source", "google"],
        cwd=BLEND_UTILS, check=True, env=env,
    )


def _upload_outputs(date: str, region: str, bucket: str) -> None:
    blend_out = Path(f"/app/blend/output_google/{date}")
    gcs_prefix = f"{region}/output/blend/{date}"
    upload_directory(bucket, blend_out, gcs_prefix)
    logger.info(f"Blend outputs uploaded to gs://{bucket}/{gcs_prefix}/")


if __name__ == "__main__":
    main()
