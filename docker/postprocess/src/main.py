"""
Monsoon Post-process — GCS Verification Gate

Each model container writes a per-(model, region) completion marker to the
COMMON bucket when it finishes uploading region outputs. This service verifies
that all markers required for FORECAST_REGION exist before blend/sync proceed.

Date convention per model:
  aifs, aifs_ens : aifs_date = DATE - 12h
  neuralgcm      : DATE (no shift)

Environment Variables:
    DATE              : NeuralGCM-paced forecast date YYYYMMDDTHH
    FORECAST_REGION   : Region whose markers to check
    GCS_COMMON_BUCKET : Common bucket holding intermediate/{model}_{region}_{date}_done markers
    REGION_MODELS     : JSON map {region: [models]} (the models required for each region)
"""

import json
import logging

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

AIFS_MODELS = {"aifs", "aifs_ens"}


def _blob_exists(bucket: str, gcs_path: str) -> bool:
    return storage.Client().bucket(bucket).blob(gcs_path).exists()



def _marker_paths(region: str, date: str, models: list[str]) -> list[str]:
    return [
        f"intermediate/{model}_{region}_{date}_done"
        for model in models
    ]


@click.command()
@click.option("--date",   envvar="DATE",            required=True)
@click.option("--region", envvar="FORECAST_REGION", required=True)
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-models", envvar="REGION_MODELS", required=True,
              help="JSON map {region: [models]}")
def main(date, region, common_bucket, region_models):
    region_models = json.loads(region_models)
    if region not in region_models:
        raise click.ClickException(f"No models configured for region {region!r}")
    models = region_models[region]
    if not models:
        raise click.ClickException(f"Region {region!r} has an empty model list")

    required = _marker_paths(region, date, models)
    missing = []
    for path in required:
        if _blob_exists(common_bucket, path):
            logger.info(f"  ✓ gs://{common_bucket}/{path}")
        else:
            logger.error(f"  ✗ MISSING: gs://{common_bucket}/{path}")
            missing.append(path)

    if missing:
        raise RuntimeError(
            f"Post-process verification failed for region={region}: "
            f"{len(missing)} marker(s) missing.\n" + "\n".join(f"  - {p}" for p in missing)
        )

    logger.info(f"All required markers verified for region={region}. Proceeding.")


if __name__ == "__main__":
    main()
