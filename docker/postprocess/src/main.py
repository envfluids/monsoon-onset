"""
Monsoon Post-process — GCS Verification Step

Verifies that all required model outputs exist in GCS before blending begins.
AIFS and NeuralGCM each run their own post-processing as part of their containers,
so this step is purely a gate check.

Environment Variables:
    DATE            : NeuralGCM date YYYYMMDDTHH
    FORECAST_REGION : e.g. 'india'
    GCS_BUCKET      : Main data bucket
"""

import logging
from datetime import datetime, timedelta

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

REQUIRED_OUTPUT_KINDS = ("sji", "tcw", "tp")


def required_output_paths(region: str, date: str, aifs_date: str) -> list[str]:
    paths = []
    for kind in REQUIRED_OUTPUT_KINDS:
        paths.append(f"{region}/output/aifs/{aifs_date}/{kind}/{kind}_{aifs_date}.nc")
    for kind in REQUIRED_OUTPUT_KINDS:
        paths.append(f"{region}/output/neuralgcm/{date}/{kind}/{kind}_{date}.nc")
    return paths


def _read_gcs_text(bucket_name: str, gcs_path: str) -> str:
    return storage.Client().bucket(bucket_name).blob(gcs_path).download_as_text().strip()


def _blob_exists(bucket_name: str, gcs_path: str) -> bool:
    return storage.Client().bucket(bucket_name).blob(gcs_path).exists()


@click.command()
@click.option("--date",   envvar="DATE",           required=True)
@click.option("--region", envvar="FORECAST_REGION", default="india")
@click.option("--bucket", envvar="GCS_BUCKET",      required=True)
def main(date, region, bucket):
    # Determine the AIFS IC date (12h before NeuralGCM date)
    try:
        ecmwf_date = _read_gcs_text(bucket, f"{region}/intermediate/latest_ecmwf_date.txt")
        logger.info(f"ECMWF date from GCS: {ecmwf_date}")
    except Exception:
        ecmwf_date = (datetime.strptime(date, "%Y%m%dT%H") - timedelta(hours=12)).strftime("%Y%m%dT%H")
        logger.warning(f"Could not read latest_ecmwf_date.txt; using computed aifs_date={ecmwf_date}")

    missing = []
    for gcs_path in required_output_paths(region, date, ecmwf_date):
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
