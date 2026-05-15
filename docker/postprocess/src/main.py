"""
Monsoon Post-process — GCS Verification Step

Verifies that all required model outputs exist in GCS before blending begins.
AIFS and NeuralGCM each run their own post-processing as part of their containers,
so this step is purely a gate check.

Environment Variables:
    DATE            : Forecast init date YYYYMMDDTHH
    FORECAST_REGION : e.g. 'india' or 'ethiopia'
    GCS_BUCKET      : Region data bucket
"""

import logging

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

def required_output_paths(region: str, date: str) -> list[str]:
    if region == "ethiopia":
        return [
            f"output/aifs/{date}/ethiopia/AIFS/tp/tp_0p25_{date}.nc",
            f"output/aifs_ens/{date}/ethiopia/AIFS_ENS/tp/tp_0p25_{date}.nc",
        ]
    if region == "india":
        return [
            f"output/aifs/{date}/india/tp/tp_0p25_{date}.nc",
            f"output/ncum/{date}/precipitation_amount/precipitation_amount_{date}.nc",
        ]
    return []


def _blob_exists(bucket_name: str, gcs_path: str) -> bool:
    return storage.Client().bucket(bucket_name).blob(gcs_path).exists()


@click.command()
@click.option("--date",   envvar="DATE",           required=True)
@click.option("--region", envvar="FORECAST_REGION", default="india")
@click.option("--bucket", envvar="GCS_BUCKET",      required=True)
def main(date, region, bucket):
    missing = []
    required_paths = required_output_paths(region, date)
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
