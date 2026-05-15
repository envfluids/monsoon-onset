"""
Monsoon Sync — GCS Shim Wrapper

Stages per-region forecast outputs from the region bucket into a local layout
that the existing sync engine (sync/utils/) understands, then syncs to Google
Drive and (optionally) git-pushes to the monsoon-operational repo. Updates the
region's latest.txt marker on success.

The shim is region-agnostic: behavior is driven by SYNC_SPEC (a JSON-encoded
copy of the region's `sync` block from terraform), so adding a region is a pure
terraform change.

Date convention:
  DATE is the unified forecast date — pipeline-state only declares a region's
  sync ready when the region's primary output (blend for india, AIFS outputs
  for ethiopia) exists at this date.  Sources may reference {date} or
  {aifs_date}; both substitute to DATE in this shim (the framework keeps the
  distinction for future use if ECMWF and NCEP cycles ever drift apart).

Environment Variables:
    DATE              : Forecast date YYYYMMDDTHH
    FORECAST_REGION   : Region whose outputs to sync
    GCS_REGION_BUCKETS: JSON map {region: bucket}
    SYNC_SPEC         : JSON copy of regions[region].sync from terraform
    ENABLE_DRIVE      : 'true' to enable Google Drive sync (default false)
    MONSOON_SYNC_CONFIG : Path to sync.yaml (default /app/sync/config/sync.yaml)
    MONSOON_CLUSTER     : Cluster name passed to sync config (default 'gcp')
    MONSOON_DRIVE_ROOT  : Override drive root (optional)
"""

import json
import logging
import os
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
        count += 1
    if count == 0:
        logger.info("No objects found at gs://%s/%s", bucket_name, gcs_prefix)
    return count


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}: {content!r}")


@click.command()
@click.option("--date",   envvar="DATE",            required=True)
@click.option("--region", envvar="FORECAST_REGION", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True,
              help="JSON map {region: bucket}")
@click.option("--sync-spec", envvar="SYNC_SPEC", required=True,
              help="JSON copy of regions[region].sync from terraform")
@click.option("--enable-drive/--no-drive", envvar="ENABLE_DRIVE", default=False)
@click.option("--config",     envvar="MONSOON_SYNC_CONFIG", default="/app/sync/config/sync.yaml")
@click.option("--cluster",    envvar="MONSOON_CLUSTER",     default="gcp")
@click.option("--drive-root", envvar="MONSOON_DRIVE_ROOT",  default=None)
def main(date, region, region_buckets, sync_spec, enable_drive, config, cluster, drive_root):
    region_buckets = json.loads(region_buckets)
    spec = json.loads(sync_spec)
    if region not in region_buckets:
        raise click.ClickException(f"No bucket configured for region {region!r}")
    region_bucket = region_buckets[region]

    logger.info(
        "Sync: region=%s date=%s rules=%s git_push=%s",
        region, date, spec["rules"], spec["git_push"],
    )

    with tempfile.TemporaryDirectory() as tmp:
        sync_root = Path(tmp) / "sync-root"
        sync_root.mkdir(parents=True)

        for src in spec["sources"]:
            gcs_prefix = src["gcs_prefix"].format(date=date, aifs_date=date)
            local_subdir = src["local_dir"].format(date=date, aifs_date=date)
            local_dir = sync_root / local_subdir
            logger.info("Staging gs://%s/%s → %s", region_bucket, gcs_prefix, local_dir)
            download_gcs_prefix(region_bucket, gcs_prefix, local_dir)

        if enable_drive:
            _run_sync_engine(sync_root, region, set(spec["rules"]), {date},
                             config, cluster, drive_root)
        else:
            logger.info("ENABLE_DRIVE=false — skipping Google Drive sync")

        if spec["git_push"]:
            _git_push_operational(sync_root, region, date)
        else:
            logger.info("git_push=false for region %s — skipping operational repo push", region)

    write_gcs_text(region_bucket, "latest.txt", date)
    logger.info(f"Sync complete for region={region} date={date}")


def _run_sync_engine(
    sync_root: Path,
    region: str,
    rule_names: set[str],
    dates: set[str],
    config: str,
    cluster: str,
    drive_root: str | None,
) -> None:
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
        summary = engine.sync(dates=dates, rule_names=rule_names)
    logger.info("Google Drive sync complete: %s", summary)


def _git_push_operational(sync_root: Path, region: str, date: str) -> None:
    """Placeholder for the india-only push to monsoon-operational.

    The HPC pipeline pushes blend outputs to a sibling repo. In the cloud,
    this hook can be wired to a Cloud Source Repository or kept idle.
    Implement when an operational repo target is provisioned for the cloud env.
    """
    logger.info(
        "git_push for region=%s date=%s requested, but no operational repo is wired up "
        "in this environment yet — no-op",
        region, date,
    )


if __name__ == "__main__":
    main()
