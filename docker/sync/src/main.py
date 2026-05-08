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
@click.option("--region",       envvar="FORECAST_REGION", required=True)
@click.option("--bucket",       envvar="GCS_BUCKET",      required=True)
@click.option("--enable-drive/--no-drive", envvar="ENABLE_DRIVE", default=False)
@click.option("--config",       envvar="MONSOON_SYNC_CONFIG", default="/app/sync/config/sync.yaml")
@click.option("--cluster",      envvar="MONSOON_CLUSTER", default="gcp")
@click.option("--drive-root",   envvar="MONSOON_DRIVE_ROOT", default=None)
def main(date, region, bucket, enable_drive, config, cluster, drive_root):
    blend_prefix = f"{region}/output/blend/{date}/"

    with tempfile.TemporaryDirectory() as tmp:
        sync_root = Path(tmp) / "sync-root"
        local_blend = sync_root / "blend" / "output_google" / region / date
        local_blend.mkdir(parents=True)

        # Download blend outputs
        logger.info(f"Downloading blend outputs from gs://{bucket}/{blend_prefix}")
        download_gcs_prefix(bucket, blend_prefix, local_blend)

        if enable_drive:
            _sync_to_drive(sync_root, date, region, config, cluster, drive_root)

    # Update the latest.txt marker
    write_gcs_text(bucket, f"{region}/latest.txt", date)
    logger.info(f"Sync complete for {date}")


def _sync_to_drive(
    sync_root: Path,
    date: str,
    region: str,
    config: str,
    cluster: str,
    drive_root: str | None,
) -> None:
    """Sync staged outputs to Google Drive through the shared sync engine."""
    from sync.utils.drive import GoogleDriveClient
    from sync.utils.sync_config import load_sync_config
    from sync.utils.sync_engine import SyncEngine
    from sync.utils.sync_inventory import SyncInventory

    sync_config = load_sync_config(
        config,
        sync_root=sync_root,
        cluster=cluster,
        drive_root=drive_root,
        region=region,
    )
    with SyncInventory(sync_config.inventory_path) as inventory:
        engine = SyncEngine(sync_config, GoogleDriveClient.authenticated(), inventory)
        summary = engine.sync(dates={date}, rule_names={"blend_google"})
    logger.info("Google Drive sync complete: %s", summary)


if __name__ == "__main__":
    main()
