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
    SYNC_ITEMS        : JSON list of incremental items to sync; blend,
                        model_diagnostics and drywetcast items narrow those
                        rules to one completed output subtree.
    MONSOON_SYNC_CONFIG : Path to sync.yaml (default /app/sync/config/sync.yaml)
    MONSOON_CLUSTER     : Cluster name passed to sync config (default 'gcp')
    MONSOON_DRIVE_ROOT  : Override drive root (optional)
    MONSOON_DRIVE_FOLDER_ID : Drive folder ID that rule paths resolve under,
                        instead of a path from the account's My Drive (optional)
    MONSOON_DRIVE_HEADLESS  : '1' to fail instead of starting a browser login
    GOOGLE_DRIVE_CREDENTIALS_JSON : Optional OAuth client JSON from Secret Manager
    GOOGLE_DRIVE_TOKEN_JSON       : Optional OAuth token JSON from Secret Manager
    ACTION              : 'run' (default) or 'probe'. probe only checks the Drive
                        login and folder: lists it, then creates, reads back and
                        removes a small test file. It touches no GCS.

Failures: a failed run releases its item claims and records the execution in
sync-items/<date>/<type>/<name>.failed.json. After SYNC_ITEM_MAX_ATTEMPTS
executions an item is given up (SYNC_ITEMS_GAVE_UP at ERROR, which alerts).
DryWetCast items for one date take a short lock so two executions don't create
the same Drive folders at once.
"""

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from google.api_core.exceptions import NotFound, PreconditionFailed
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
SYNC_ITEM_TYPES = {"blend", "model_diagnostics", "drywetcast"}
# Same as pipeline-state's ACTIVE_SYNC_MAX_AGE_SECONDS: past 3 attempts x 600 s,
# so an older claim is from a killed execution and may be taken over.
SYNC_ITEM_CLAIM_TTL_SECONDS = int(os.environ.get("SYNC_ITEM_CLAIM_TTL_SECONDS", str(45 * 60)))
SYNC_ITEM_MAX_ATTEMPTS = int(os.environ.get("SYNC_ITEM_MAX_ATTEMPTS", "3"))
SYNC_WORKERS = int(os.environ.get("SYNC_WORKERS", "1"))
# Item types whose syncs share Drive folders per date and so run one at a time.
LOCKED_ITEM_TYPES = {"drywetcast"}
SYNC_LOCK_WAIT_SECONDS = int(os.environ.get("SYNC_LOCK_WAIT_SECONDS", "120"))
SYNC_LOCK_POLL_SECONDS = int(os.environ.get("SYNC_LOCK_POLL_SECONDS", "5"))
SYNC_LOCK_STALE_SECONDS = int(os.environ.get("SYNC_LOCK_STALE_SECONDS", str(15 * 60)))
PROBE_FILE_TEXT = b"monsoon sync probe; safe to delete\n"


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


def read_gcs_json(bucket_name: str, gcs_path: str) -> dict:
    blob = _client().bucket(bucket_name).blob(gcs_path)
    try:
        text = blob.download_as_text()
    except NotFound:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid sync marker JSON: gs://%s/%s", bucket_name, gcs_path)
        return {}
    return parsed if isinstance(parsed, dict) else {}


@click.command()
@click.option("--action", envvar="ACTION", default="run",
              type=click.Choice(["run", "probe"]))
@click.option("--date",   envvar="DATE",            default="")
@click.option("--region", envvar="FORECAST_REGION", default="")
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", default="",
              help="JSON map {region: bucket}")
@click.option("--sync-spec", envvar="SYNC_SPEC", default="",
              help="JSON copy of regions[region].sync from terraform")
@click.option("--enable-drive/--no-drive", envvar="ENABLE_DRIVE", default=False)
@click.option("--sync-fingerprint", envvar="SYNC_FINGERPRINT", default="")
@click.option("--sync-items", envvar="SYNC_ITEMS", default="")
@click.option("--config",     envvar="MONSOON_SYNC_CONFIG", default="/app/sync/config/sync.yaml")
@click.option("--cluster",    envvar="MONSOON_CLUSTER",     default="gcp")
@click.option("--drive-root", envvar="MONSOON_DRIVE_ROOT",  default=None)
def main(
    action,
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
    if action == "probe":
        sys.exit(probe())
    missing = [
        name
        for name, value in (
            ("DATE", date),
            ("FORECAST_REGION", region),
            ("GCS_REGION_BUCKETS", region_buckets),
            ("SYNC_SPEC", sync_spec),
        )
        if not value
    ]
    if missing:
        raise click.ClickException(f"Missing required settings: {', '.join(missing)}")
    if os.environ.get("MONSOON_DRIVE_FOLDER_ID"):
        # Rule paths ({drive_root}/...) then resolve inside that folder.
        drive_root = "/"

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
        requested_sync_items = _parse_sync_items(sync_items)
        claimed_sync_items = _claim_sync_items(region_bucket, region, date, requested_sync_items)
        if requested_sync_items and not claimed_sync_items:
            logger.info(
                "No unclaimed sync items for region=%s date=%s; another execution already synced or claimed them",
                region,
                date,
            )
            return

        lock_path = ""
        try:
            if any(item.get("type") in LOCKED_ITEM_TYPES for item in claimed_sync_items):
                lock_path = _acquire_date_lock(region_bucket, date, "drywetcast")

            rule_names = _rule_names_for_sync(spec["rules"], claimed_sync_items or requested_sync_items)
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
                claimed_sync_items or requested_sync_items,
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
        except Exception as exc:
            _record_sync_failure(region_bucket, region, date, claimed_sync_items, exc)
            raise
        finally:
            if lock_path:
                _release_date_lock(region_bucket, lock_path)

    if claimed_sync_items:
        _mark_sync_items_done(region_bucket, region, date, claimed_sync_items)

    write_gcs_text(region_bucket, "latest.txt", date)
    if sync_fingerprint and not requested_sync_items:
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


def _rule_names_for_sync(configured_rules: list[str], sync_items: list[dict]) -> set[str]:
    if not sync_items:
        return set(configured_rules)
    requested_rules = {
        item["type"]
        for item in sync_items
        if item.get("type") in SYNC_ITEM_TYPES
    }
    return set(configured_rules) & requested_rules


def _claim_sync_items(
    bucket_name: str,
    region: str,
    date: str,
    sync_items: list[dict],
) -> list[dict]:
    if not sync_items:
        return []

    claimed = []
    for item in sync_items:
        if item.get("type") not in SYNC_ITEM_TYPES:
            claimed.append(item)
            continue
        if _sync_item_done(bucket_name, date, item):
            logger.info(
                "Skipping already-synced item: region=%s date=%s type=%s name=%s",
                region,
                date,
                item["type"],
                item["name"],
            )
            continue
        if _sync_item_attempts(bucket_name, date, item) >= SYNC_ITEM_MAX_ATTEMPTS:
            logger.info(
                "Skipping given-up item: region=%s date=%s type=%s name=%s",
                region,
                date,
                item["type"],
                item["name"],
            )
            continue
        if _claim_sync_item(bucket_name, region, date, item):
            claimed.append(item)
    return claimed


def _sync_item_done(bucket_name: str, date: str, item: dict) -> bool:
    return _client().bucket(bucket_name).blob(_sync_item_marker_path(date, item, "done")).exists()


def _claim_sync_item(bucket_name: str, region: str, date: str, item: dict) -> bool:
    active_path = _sync_item_marker_path(date, item, "active")
    blob = _client().bucket(bucket_name).blob(active_path)
    payload = _sync_item_marker_payload(region, date, item)
    try:
        blob.upload_from_string(payload, if_generation_match=0)
        logger.info(
            "Claimed sync item: region=%s date=%s type=%s name=%s",
            region,
            date,
            item["type"],
            item["name"],
        )
        return True
    except PreconditionFailed:
        if not _sync_item_claim_is_stale(bucket_name, active_path):
            logger.info(
                "Skipping active sync item: region=%s date=%s type=%s name=%s",
                region,
                date,
                item["type"],
                item["name"],
            )
            return False
        blob.reload()
        blob.upload_from_string(payload, if_generation_match=blob.generation)
        logger.warning(
            "Reclaimed stale sync item: region=%s date=%s type=%s name=%s",
            region,
            date,
            item["type"],
            item["name"],
        )
        return True


def _sync_item_claim_is_stale(bucket_name: str, active_path: str) -> bool:
    marker = read_gcs_json(bucket_name, active_path)
    updated_at = _parse_marker_time(marker.get("updated_at")) or _parse_marker_time(
        marker.get("started_at")
    )
    if updated_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return age_seconds > SYNC_ITEM_CLAIM_TTL_SECONDS


def _mark_sync_items_done(bucket_name: str, region: str, date: str, sync_items: list[dict]) -> None:
    for item in sync_items:
        if item.get("type") not in SYNC_ITEM_TYPES:
            continue
        write_gcs_text(
            bucket_name,
            _sync_item_marker_path(date, item, "done"),
            _sync_item_marker_payload(region, date, item),
        )


def _execution_id() -> str:
    return os.environ.get("CLOUD_RUN_EXECUTION", f"local-{os.getpid()}")


def _sync_item_attempts(bucket_name: str, date: str, item: dict) -> int:
    marker = read_gcs_json(bucket_name, _sync_item_marker_path(date, item, "failed"))
    return len(marker.get("executions", []))


def _record_sync_failure(
    bucket_name: str,
    region: str,
    date: str,
    sync_items: list[dict],
    error: Exception,
) -> None:
    """Release the claims so a later pass can retry, and count this execution.

    Attempts count executions, not Cloud Run task retries, so an item gets
    SYNC_ITEM_MAX_ATTEMPTS separate passes before it is given up.
    """
    items = [item for item in sync_items if item.get("type") in SYNC_ITEM_TYPES]
    if not items:
        return
    execution = _execution_id()
    gave_up = []
    for item in items:
        failed_path = _sync_item_marker_path(date, item, "failed")
        marker = read_gcs_json(bucket_name, failed_path)
        executions = list(marker.get("executions", []))
        if execution not in executions:
            executions.append(execution)
        write_gcs_text(
            bucket_name,
            failed_path,
            json.dumps(
                {
                    "region": region,
                    "date": date,
                    "type": item["type"],
                    "name": item["name"],
                    "executions": executions,
                    "last_error": str(error)[:500],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                sort_keys=True,
            ),
        )
        try:
            _client().bucket(bucket_name).blob(
                _sync_item_marker_path(date, item, "active")
            ).delete()
        except NotFound:
            pass
        if len(executions) >= SYNC_ITEM_MAX_ATTEMPTS:
            gave_up.append(f"{item['type']}/{item['name']}")
    names = ", ".join(f"{item['type']}/{item['name']}" for item in items)
    logger.error(
        "SYNC_FAILED region=%s date=%s items=%s: %s", region, date, names, error
    )
    if gave_up:
        logger.error(
            "SYNC_ITEMS_GAVE_UP region=%s date=%s items=%s after %d failed executions",
            region,
            date,
            ", ".join(gave_up),
            SYNC_ITEM_MAX_ATTEMPTS,
        )


def _date_lock_path(date: str, item_type: str) -> str:
    return f"sync-items/{date}/{item_type}/_lock.json"


def _acquire_date_lock(bucket_name: str, date: str, item_type: str) -> str:
    """Create-if-absent lock; wait for another execution, take over a stale lock."""
    path = _date_lock_path(date, item_type)
    blob = _client().bucket(bucket_name).blob(path)
    payload = json.dumps(
        {
            "execution": _execution_id(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    deadline = time.monotonic() + SYNC_LOCK_WAIT_SECONDS
    while True:
        try:
            blob.upload_from_string(payload, if_generation_match=0)
            return path
        except PreconditionFailed:
            pass
        marker = read_gcs_json(bucket_name, path)
        started = _parse_marker_time(marker.get("started_at"))
        age = (
            (datetime.now(timezone.utc) - started).total_seconds()
            if started
            else SYNC_LOCK_STALE_SECONDS + 1
        )
        if age > SYNC_LOCK_STALE_SECONDS:
            try:
                blob.reload()
                blob.upload_from_string(payload, if_generation_match=blob.generation)
                logger.warning("Took over a stale sync lock: %s", path)
                return path
            except (NotFound, PreconditionFailed):
                continue
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"Timed out after {SYNC_LOCK_WAIT_SECONDS} s waiting for sync lock {path}"
            )
        time.sleep(SYNC_LOCK_POLL_SECONDS)


def _release_date_lock(bucket_name: str, path: str) -> None:
    try:
        _client().bucket(bucket_name).blob(path).delete()
    except NotFound:
        pass


def probe() -> int:
    """Check the Drive login and folder; create, read back and remove a test file."""
    result = {"folder_id": os.environ.get("MONSOON_DRIVE_FOLDER_ID", "root")}
    step = "credentials"
    try:
        os.environ.setdefault("MONSOON_DRIVE_HEADLESS", "1")
        _materialize_drive_auth()
        from sync.utils.drive import GoogleDriveClient

        drive = GoogleDriveClient.authenticated()
        result["token_ok"] = True
        step = "account"
        result["account"] = drive.account_email()
        step = "folder"
        folder = drive.folder_info()
        result.update(
            folder_name=folder.get("name"),
            shared_drive=bool(folder.get("driveId")),
            can_add_children=True,
        )
        step = "list"
        result["first_children"] = sorted(item["name"] for item in drive.list_children())
        step = "create"
        name = (
            f"_monsoon_sync_probe_{_execution_id()}_"
            f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.txt"
        )
        created = drive.create_small_file(name, PROBE_FILE_TEXT)
        result["test_file"] = name
        step = "read_back"
        fetched = drive.get_file(created["id"])
        result["readback_ok"] = int(fetched.get("size", -1)) == len(PROBE_FILE_TEXT)
        step = "remove"
        result["removed"] = drive.remove_file(created["id"])
        result["ok"] = result["readback_ok"]
    except Exception as exc:  # noqa: BLE001 - any failure is the probe result
        result.update(ok=False, failed_step=step, error=str(exc)[:500])
    logger.info("SYNC_PROBE %s", json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


def _sync_item_marker_payload(region: str, date: str, item: dict) -> str:
    now = datetime.now(timezone.utc).isoformat()
    return json.dumps(
        {
            "region": region,
            "date": date,
            "type": item["type"],
            "name": item["name"],
            "started_at": now,
            "updated_at": now,
        },
        sort_keys=True,
    )


def _sync_item_marker_path(date: str, item: dict, state: str) -> str:
    return f"sync-items/{date}/{item['type']}/{_safe_marker_name(item['name'])}.{state}.json"


def _safe_marker_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def _parse_marker_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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

    if rule.name not in ITEM_SUBPATHS:
        return [(gcs_prefix, local_dir)]

    # Each item type's rule is named after it.
    selected = [item["name"] for item in sync_items if item.get("type") == rule.name]
    if not selected:
        return [(gcs_prefix, local_dir)]

    targets = []
    for name in sorted(set(selected)):
        subpath = ITEM_SUBPATHS[rule.name](date, name)
        targets.append((f"{gcs_prefix.rstrip('/')}/{subpath}/", local_dir / subpath))
    return targets


# Where one item's outputs sit under its rule's gcs_prefix (and staging dir).
# blend and model_diagnostics prefixes stop above the date; drywetcast's
# (output/drywetcast/{date}/) and its staging dir already include it.
ITEM_SUBPATHS = {
    "blend": lambda date, name: f"{date}/{name}",
    "model_diagnostics": lambda date, name: f"{date}/{name}",
    "drywetcast": lambda date, name: name,
}


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

    drive = GoogleDriveClient.authenticated()
    if drive.root_folder_id != "root":
        folder = drive.folder_info()
        logger.info("Syncing under Drive folder %s (%r)", folder["id"], folder.get("name"))
    with SyncInventory(sync_config.inventory_path) as inventory:
        engine = SyncEngine(sync_config, drive, inventory)
        discovered = engine.discover(dates=dates, rule_names=rule_names)
        discovered_rules = {item.rule for item in discovered}
        empty_rules = sorted(rule_names - discovered_rules)
        if empty_rules:
            raise click.ClickException(
                f"Sync discovered no local files for requested rule(s): {empty_rules}"
            )
        summary = engine.sync(dates=dates, rule_names=rule_names, workers=SYNC_WORKERS)
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
