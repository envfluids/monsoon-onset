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
                        (selects sync.yaml rule names and git behavior)
    ENABLE_DRIVE      : 'true' to enable Google Drive sync (default false)
    SYNC_FINGERPRINT  : Serialized sync inventory fingerprint to persist after success
    SYNC_ITEMS        : JSON list of incremental items to sync; blend and
                        model_diagnostics items narrow those rules to one
                        completed config output subtree.
    MONSOON_SYNC_CONFIG : Path to sync.yaml (default /app/sync/config/sync.yaml)
    MONSOON_CLUSTER     : Cluster name passed to sync config (default 'gcp')
    MONSOON_DRIVE_ROOT  : Override drive root (optional)
    GOOGLE_DRIVE_CREDENTIALS_JSON : Optional OAuth client JSON from Secret Manager
    GOOGLE_DRIVE_TOKEN_JSON       : Optional OAuth token JSON from Secret Manager
"""

import json
import logging
import os
import tempfile
from pathlib import Path

import click
from google.cloud import storage

from sync.utils.sync_config import SyncConfig, SyncRule, load_sync_config
from sync.utils.sync_engine import SyncEngine
from sync.utils.sync_inventory import SyncInventory

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

DRIVE_AUTH_DIR = Path("/app/sync/.auth")
DRIVE_AUTH_ENV_FILES = {
    "GOOGLE_DRIVE_CREDENTIALS_JSON": "credentials.json",
    "GOOGLE_DRIVE_TOKEN_JSON": "token.json",
}


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
@click.option("--sync-fingerprint", envvar="SYNC_FINGERPRINT", default="")
@click.option("--sync-items", envvar="SYNC_ITEMS", default="")
@click.option("--config",     envvar="MONSOON_SYNC_CONFIG", default="/app/sync/config/sync.yaml")
@click.option("--cluster",    envvar="MONSOON_CLUSTER",     default="gcp")
@click.option("--drive-root", envvar="MONSOON_DRIVE_ROOT",  default=None)
def main(
    date,
    region,
    region_buckets,
    sync_spec,
    enable_drive,
    sync_fingerprint,
    sync_items,
    config,
    cluster,
    drive_root,
):
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
        rule_names = set(spec["rules"])
        sync_config = load_sync_config(
            config,
            sync_root=sync_root,
            cluster=cluster,
            drive_root=drive_root,
            region=region,
        )

        staged_rule_names = _stage_sync_rules(
            region_bucket,
            sync_config,
            rule_names,
            date,
            _parse_sync_items(sync_items),
        )

        if enable_drive:
            _materialize_drive_auth()
            _run_sync_engine(sync_config, staged_rule_names, {date})
        else:
            logger.info("ENABLE_DRIVE=false — skipping Google Drive sync")

        if spec["git_push"]:
            _git_push_operational(sync_root, region, date)
        else:
            logger.info("git_push=false for region %s — skipping operational repo push", region)

    write_gcs_text(region_bucket, "latest.txt", date)
    if sync_fingerprint:
        write_gcs_text(region_bucket, f"sync-state/{date}.json", sync_fingerprint)
    logger.info(f"Sync complete for region={region} date={date}")


def _materialize_drive_auth() -> None:
    """Write Drive OAuth JSON env secrets to the legacy auth file locations."""
    wrote_any = False
    for env_name, file_name in DRIVE_AUTH_ENV_FILES.items():
        raw = os.environ.get(env_name)
        if not raw:
            continue
        parsed = json.loads(raw)
        DRIVE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
        target = DRIVE_AUTH_DIR / file_name
        target.write_text(json.dumps(parsed), encoding="utf-8")
        target.chmod(0o600)
        wrote_any = True

    if wrote_any:
        logger.info("Materialized Google Drive auth files from Secret Manager env vars")


def _stage_sync_rules(
    region_bucket: str,
    sync_config: SyncConfig,
    rule_names: set[str],
    date: str,
    sync_items: list[dict],
) -> set[str]:
    rules_by_name = {rule.name: rule for rule in sync_config.rules}
    missing_rules = sorted(rule_names - set(rules_by_name))
    if missing_rules:
        raise click.ClickException(
            f"SYNC_SPEC requested rule(s) not defined for {sync_config.region}: {missing_rules}"
        )

    staged_rule_names = set()
    for rule_name in sorted(rule_names):
        rule = rules_by_name[rule_name]
        if not rule.gcs_prefix:
            raise click.ClickException(
                f"Sync rule {sync_config.region}/{rule.name} is missing gcs_prefix; "
                "cloud sync cannot stage it from GCS."
            )
        downloaded_any = False
        for gcs_prefix, local_dir in _staging_targets_for_rule(
            rule,
            sync_config,
            date,
            sync_items,
        ):
            logger.info(
                "Staging rule=%s gs://%s/%s → %s",
                rule.name,
                region_bucket,
                gcs_prefix,
                local_dir,
            )
            downloaded = download_gcs_prefix(region_bucket, gcs_prefix, local_dir)
            if downloaded == 0:
                logger.info(
                    "Skipping sync rule %s/%s; no GCS objects at gs://%s/%s",
                    sync_config.region,
                    rule.name,
                    region_bucket,
                    gcs_prefix,
                )
                continue
            downloaded_any = True
        if downloaded_any:
            staged_rule_names.add(rule.name)

    if not staged_rule_names:
        raise click.ClickException(
            f"None of the requested sync rules had GCS objects for {sync_config.region}/{date}"
        )
    return staged_rule_names


def _parse_sync_items(raw: str) -> list[dict]:
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise click.ClickException("SYNC_ITEMS must be a JSON list")
    items = []
    for item in parsed:
        if not isinstance(item, dict):
            raise click.ClickException("SYNC_ITEMS entries must be objects")
        item_type = item.get("type", "")
        item_name = item.get("name", "")
        if isinstance(item_type, str) and isinstance(item_name, str) and item_type and item_name:
            items.append({"type": item_type, "name": item_name})
    return items


def _staging_targets_for_rule(
    rule: SyncRule,
    sync_config: SyncConfig,
    date: str,
    sync_items: list[dict],
) -> list[tuple[str, Path]]:
    gcs_date = _date_for_rule(rule, date)
    gcs_prefix = _render_template(rule.gcs_prefix, sync_config, date=gcs_date)
    stage_template = rule.gcs_stage_dir or rule.local_root
    local_dir = sync_config.sync_root / _render_template(stage_template, sync_config, date=date)

    if rule.name not in {"blend", "model_diagnostics"}:
        return [(gcs_prefix, local_dir)]

    item_type = "model_diagnostics" if rule.name == "model_diagnostics" else "blend"
    selected = [item["name"] for item in sync_items if item.get("type") == item_type]
    if not selected:
        return [(gcs_prefix, local_dir)]

    targets = []
    for name in sorted(set(selected)):
        subpath = f"{date}/{name}/"
        targets.append((f"{gcs_prefix.rstrip('/')}/{subpath}", local_dir / date / name))
    return targets


def _date_for_rule(rule: SyncRule, date: str) -> str:
    # The workflow currently passes one resolved date; the kind is retained so
    # rule metadata stays explicit if ECMWF/NCEP dates diverge later.
    _ = rule.gcs_date_kind
    return date


def _render_template(value: str, sync_config: SyncConfig, *, date: str) -> str:
    return value.format(
        date=date,
        aifs_date=date,
        region=sync_config.region,
        cluster=sync_config.cluster,
        drive_root=sync_config.drive_root,
    )


def _run_sync_engine(
    sync_config: SyncConfig,
    rule_names: set[str],
    dates: set[str],
) -> None:
    from sync.utils.drive import GoogleDriveClient

    with SyncInventory(sync_config.inventory_path) as inventory:
        engine = SyncEngine(sync_config, GoogleDriveClient.authenticated(), inventory)
        discovered = engine.discover(dates=dates, rule_names=rule_names)
        discovered_rules = {item.rule for item in discovered}
        empty_rules = sorted(rule_names - discovered_rules)
        if empty_rules:
            raise click.ClickException(
                f"Sync discovered no local files for requested rule(s): {empty_rules}"
            )
        summary = engine.sync(dates=dates, rule_names=rule_names)
        if summary.errors:
            raise click.ClickException(
                f"Google Drive sync finished with {summary.errors} error(s)"
            )
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
