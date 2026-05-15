"""
Monsoon Post-process — GCS Verification Step

Verifies that all required model outputs exist in GCS before blending begins.
AIFS and NeuralGCM each run their own post-processing as part of their containers,
so this step is purely a gate check.

Environment Variables:
    DATE            : NeuralGCM date YYYYMMDDTHH
    FORECAST_REGION : e.g. 'india'
    GCS_BUCKET      : Region data bucket
    GCS_COMMON_BUCKET : Common data bucket for shared markers
"""

import logging
import os
from datetime import datetime, timedelta

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

def required_output_paths(region: str, date: str, aifs_date: str) -> list[str]:
    if region == "ethiopia":
        return [
            f"output/aifs/{aifs_date}/ethiopia/AIFS/tp/tp_0p25_{aifs_date}.nc",
            f"output/aifs_ens/{aifs_date}/ethiopia/AIFS_ENS/tp/tp_0p25_{aifs_date}.nc",
        ]
    if region == "india":
        return [
            f"output/aifs/{aifs_date}/india/tp/tp_0p25_{aifs_date}.nc",
            f"output/ncum/{date}/precipitation_amount/precipitation_amount_{date}.nc",
        ]
    return []


def _read_gcs_text(bucket_name: str, gcs_path: str) -> str:
    return storage.Client().bucket(bucket_name).blob(gcs_path).download_as_text().strip()


def _blob_exists(bucket_name: str, gcs_path: str) -> bool:
    return storage.Client().bucket(bucket_name).blob(gcs_path).exists()


@click.command()
@click.option("--date",   envvar="DATE",           required=True)
@click.option("--region", envvar="FORECAST_REGION", default="india")
@click.option("--bucket", envvar="GCS_BUCKET",      required=True)
def main(date, region, bucket):
    common_bucket = os.environ.get("GCS_COMMON_BUCKET", bucket)
    # Determine the AIFS IC date (12h before NeuralGCM date)
    try:
        ecmwf_date = _read_gcs_text(common_bucket, "intermediate/latest_ecmwf_date.txt")
        logger.info(f"ECMWF date from GCS: {ecmwf_date}")
    except Exception:
        ecmwf_date = (datetime.strptime(date, "%Y%m%dT%H") - timedelta(hours=12)).strftime("%Y%m%dT%H")
        logger.warning(f"Could not read latest_ecmwf_date.txt; using computed aifs_date={ecmwf_date}")

    missing = []
    required_paths = required_output_paths(region, date, ecmwf_date)
    if not required_paths:
        raise RuntimeError(f"No blend input requirements are configured for region={region!r}")

    for gcs_path in required_paths:
        if _blob_exists(bucket, gcs_path):
            logger.info(f"  ✓ gs://{bucket}/{gcs_path}")
        else:
            logger.error(f"  ✗ MISSING: gs://{bucket}/{gcs_path}")
            missing.append(gcs_path)

    if missing:
        raise RuntimeError(
            f"Post-process verification failed: {len(missing)} required output(s) missing in GCS.\n"
            + "\n".join(f"  - {p}" for p in missing)
        )

    logger.info("All required model outputs verified in GCS. Proceeding to blend.")


if __name__ == "__main__":
    main()
