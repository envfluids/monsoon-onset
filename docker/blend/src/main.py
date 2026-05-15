"""
Monsoon Blend - GCS Shim Wrapper

Downloads model post-processed forecast outputs from GCS into the directory
structure expected by blend/utils/main.py, runs any configured blend whose
inputs are present, then uploads blend outputs to GCS.

Environment Variables:
    DATE               : Forecast date YYYYMMDDTHH
    FORECAST_REGION    : e.g. 'india' or 'ethiopia'
    GCS_BUCKET         : Region data bucket
    GCS_WEIGHTS_BUCKET : Weights/static-files bucket
"""

import logging
import os
import subprocess
import sys
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


def _client():
    return storage.Client()


def download_gcs_prefix(bucket_name: str, gcs_prefix: str, local_dir: Path) -> None:
    client = _client()
    found = False
    for blob in client.list_blobs(bucket_name, prefix=gcs_prefix):
        relative = blob.name[len(gcs_prefix):].lstrip("/")
        if not relative:
            continue
        found = True
        local_path = local_dir / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
        logger.info("Downloaded gs://%s/%s -> %s", bucket_name, blob.name, local_path)
    if not found:
        logger.info("No objects found at gs://%s/%s", bucket_name, gcs_prefix)


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    if not local_dir.exists():
        logger.info("Blend output directory does not exist, skipping upload: %s", local_dir)
        return
    client = _client()
    bucket = client.bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info("Uploaded %s -> gs://%s/%s", local_file, bucket_name, gcs_path)


@click.command()
@click.option("--date",           envvar="DATE",              required=True)
@click.option("--region",         envvar="FORECAST_REGION",   default="india")
@click.option("--bucket",         envvar="GCS_BUCKET",        required=True)
@click.option("--weights-bucket", envvar="GCS_WEIGHTS_BUCKET", required=True)
def main(date, region, bucket, weights_bucket):
    _setup_directories()
    _download_inputs(date, region, bucket, weights_bucket)
    _run_science_script(date, region)
    _upload_outputs(date, region, bucket)


def _setup_directories() -> None:
    for path in [
        "/app/AIFS/output",
        "/app/NCUM/output",
        "/app/NeuralGCM/output",
        "/app/blend/data/support/large",
        "/app/blend/output",
    ]:
        Path(path).mkdir(parents=True, exist_ok=True)


def _download_inputs(date: str, region: str, bucket: str, weights_bucket: str) -> None:
    model_prefixes = {
        "aifs": Path("/app/AIFS/output"),
        "aifs_ens": Path("/app/AIFS/output"),
        "ncum": Path("/app/NCUM/output"),
        "neuralgcm": Path("/app/NeuralGCM/output"),
    }
    for model, local_dir in model_prefixes.items():
        download_gcs_prefix(
            bucket,
            f"output/{model}/{date}/",
            local_dir,
        )

    download_gcs_prefix(
        weights_bucket,
        "blend/support/large/",
        Path("/app/blend/data/support/large"),
    )


def _run_science_script(date: str, region: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(BLEND_UTILS)}
    command = [sys.executable, "main.py", "--date", date, "--region", region]
    logger.info("Running blend command: %s", " ".join(command))
    subprocess.run(command, cwd=BLEND_UTILS, check=True, env=env)


def _upload_outputs(date: str, region: str, bucket: str) -> None:
    output_root = Path("/app/blend/output")
    upload_directory(bucket, output_root, f"output/blend/{date}")
    logger.info("Blend outputs uploaded to gs://%s/output/blend/%s/", bucket, date)


if __name__ == "__main__":
    main()
