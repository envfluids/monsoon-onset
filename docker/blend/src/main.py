"""
Monsoon Blend — GCS Shim Wrapper

Downloads per-region model post-processed outputs from the REGION bucket and
large blend support files from the COMMON bucket, runs the configured blend
(india uses AIFS + NeuralGCM via blend/utils/india2025/main.py), then uploads
blend outputs back to the region bucket.

Date convention:
  DATE = blend date (both AIFS and NeuralGCM produced outputs keyed on this
         same date; pipeline-state only declares blend ready when markers for
         both models at this date exist).
The shim renames the AIFS file locally from tp_2p0_{date}.nc to tp_{date}.nc
to satisfy the single-date blend scripts in blend/utils/ unchanged.

Environment Variables:
    DATE              : NeuralGCM-paced forecast date YYYYMMDDTHH
    FORECAST_REGION   : Region whose blend to run
    GCS_COMMON_BUCKET : Common bucket (large blend supports under weights/blend/{region}/...)
    GCS_REGION_BUCKETS: JSON map {region: bucket} for model outputs and blend output
"""

import json
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

REPO_ROOT   = Path("/app")
BLEND_UTILS = REPO_ROOT / "blend" / "utils"

# Local layout that the india2025 blend scripts expect
LOCAL_AIFS_TP = REPO_ROOT / "AIFS" / "output" / "tp"
LOCAL_NGCM_TP = REPO_ROOT / "NeuralGCM" / "output" / "tp"
LOCAL_BLEND_OUT_BASE = REPO_ROOT / "blend" / "output"
LOCAL_SUPPORT_LARGE = REPO_ROOT / "blend" / "data" / "support" / "large"


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def download_gcs_prefix(bucket_name: str, gcs_prefix: str, local_dir: Path) -> int:
    client = _client()
    count = 0
    for blob in client.list_blobs(bucket_name, prefix=gcs_prefix):
        relative = blob.name[len(gcs_prefix):].lstrip("/")
        if not relative:
            continue
        local_path = local_dir / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
        logger.info("Downloaded gs://%s/%s → %s", bucket_name, blob.name, local_path)
        count += 1
    if count == 0:
        logger.info("No objects found at gs://%s/%s", bucket_name, gcs_prefix)
    return count


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    if not local_dir.exists():
        logger.info("Blend output directory does not exist, skipping upload: %s", local_dir)
        return
    bucket = _client().bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info("Uploaded %s → gs://%s/%s", local_file, bucket_name, gcs_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",   envvar="DATE",            required=True)
@click.option("--region", envvar="FORECAST_REGION", required=True)
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True,
              help="JSON map {region: bucket}")
def main(date, region, common_bucket, region_buckets):
    region_buckets = json.loads(region_buckets)
    if region not in region_buckets:
        raise click.ClickException(f"No bucket configured for region {region!r}")
    region_bucket = region_buckets[region]

    logger.info(f"Blend shim: region={region} date={date}")

    if region != "india":
        raise click.ClickException(f"Blend is not configured for region {region!r}")

    _setup_directories()
    _download_inputs(date, region, region_bucket, common_bucket)
    _run_blend(date, region)
    _upload_outputs(date, region, region_bucket)


def _setup_directories() -> None:
    for d in [
        LOCAL_AIFS_TP,
        LOCAL_NGCM_TP,
        LOCAL_BLEND_OUT_BASE,
        LOCAL_SUPPORT_LARGE,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def _download_inputs(
    date: str,
    region: str,
    region_bucket: str,
    common_bucket: str,
) -> None:
    # AIFS 2-degree TP — rename tp_2p0_{date}.nc to tp_{date}.nc to satisfy blend
    download_gcs_file(
        region_bucket,
        f"output/aifs/{date}/tp/tp_2p0_{date}.nc",
        LOCAL_AIFS_TP / f"tp_{date}.nc",
    )

    # NeuralGCM 2-degree TP
    download_gcs_file(
        region_bucket,
        f"output/neuralgcm/{date}/tp/tp_2p0_{date}.nc",
        LOCAL_NGCM_TP / f"tp_{date}.nc",
    )

    # Large blend support files (ensemble climatology, gitignored due to size)
    downloaded = download_gcs_prefix(
        common_bucket,
        f"weights/blend/{region}/support/large/",
        LOCAL_SUPPORT_LARGE,
    )
    if downloaded == 0:
        logger.warning(
            "No large blend supports found at gs://%s/weights/blend/%s/support/large/ — "
            "blend will fail if it needs these files",
            common_bucket, region,
        )


def _run_blend(date: str, region: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(BLEND_UTILS)}
    command = [sys.executable, "main.py", "--date", date, "--region", region]
    logger.info("Running blend: %s", " ".join(command))
    subprocess.run(command, cwd=BLEND_UTILS, check=True, env=env)


def _upload_outputs(date: str, region: str, region_bucket: str) -> None:
    # india2025 writes to blend/output/india2025/ — upload everything under blend/output
    upload_directory(region_bucket, LOCAL_BLEND_OUT_BASE, f"output/blend/{date}")
    logger.info("Blend outputs uploaded to gs://%s/output/blend/%s/", region_bucket, date)


if __name__ == "__main__":
    main()
