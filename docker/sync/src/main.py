"""
Monsoon Sync — GCS Shim Wrapper

Copies blend outputs to a public GCS location, optionally syncs to Google
Drive, and updates the latest.txt marker.

Environment Variables:
    DATE            : Forecast date YYYYMMDDTHH
    FORECAST_REGION : e.g. 'india'
    GCS_BUCKET      : Main data bucket
    ENABLE_DRIVE    : 'true' to sync to Google Drive (requires credentials)
"""

import logging
import tempfile
from pathlib import Path

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


def _client():
    return storage.Client()


def download_gcs_prefix(bucket_name: str, gcs_prefix: str, local_dir: Path) -> None:
    client = _client()
    for blob in client.list_blobs(bucket_name, prefix=gcs_prefix):
        relative = blob.name[len(gcs_prefix):].lstrip("/")
        if not relative:
            continue
        local_path = local_dir / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}")


@click.command()
@click.option("--date",         envvar="DATE",           required=True)
@click.option("--region",       envvar="FORECAST_REGION", default="india")
@click.option("--bucket",       envvar="GCS_BUCKET",      required=True)
@click.option("--enable-drive/--no-drive", envvar="ENABLE_DRIVE", default=False)
def main(date, region, bucket, enable_drive):
    blend_prefix = f"{region}/output/blend/{date}/"

    with tempfile.TemporaryDirectory() as tmp:
        local_blend = Path(tmp) / "blend"
        local_blend.mkdir()

        # Download blend outputs
        logger.info(f"Downloading blend outputs from gs://{bucket}/{blend_prefix}")
        download_gcs_prefix(bucket, blend_prefix, local_blend)

        if enable_drive:
            _sync_to_drive(local_blend, date, region)

    # Update the latest.txt marker
    write_gcs_text(bucket, f"{region}/latest.txt", date)
    logger.info(f"Sync complete for {date}")


def _sync_to_drive(local_dir: Path, date: str, region: str) -> None:
    """Sync outputs to Google Drive using the original drive.py science script."""
    import sys
    sys.path.insert(0, "/app/sync/utils")
    try:
        import drive
        service = drive.authenticate()
        drive.drive_sync(date, cluster="gcp")
        logger.info("Google Drive sync complete")
    except Exception as e:
        logger.error(f"Google Drive sync failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
