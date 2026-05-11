from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import fnmatch
import logging
import re
import threading

try:
    from .sync_config import SyncConfig, SyncRule
    from .sync_inventory import InventoryRecord, SyncInventory
except ImportError:  # pragma: no cover - supports python sync/utils/main.py
    from sync_config import SyncConfig, SyncRule
    from sync_inventory import InventoryRecord, SyncInventory


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalSyncItem:
    region: str
    rule: str
    date: str
    local_path: Path
    relative_path: str
    drive_path: str
    file_name: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SyncSummary:
    discovered: int = 0
    uploaded: int = 0
    skipped_remote: int = 0
    skipped_inventory: int = 0
    missing_remote: int = 0
    remote_only: int = 0
    changed_local: int = 0
    errors: int = 0


class SyncEngine:
    def __init__(self, config: SyncConfig, drive_client, inventory: SyncInventory):
        self.config = config
        self.drive = drive_client
        self.inventory = inventory

    def discover(
        self,
        *,
        dates: set[str] | None = None,
        rule_names: set[str] | None = None,
    ) -> list[LocalSyncItem]:
        items: list[LocalSyncItem] = []
        for rule in self.config.rules:
            if rule_names and rule.name not in rule_names:
                continue
            rule_items = _discover_rule(self.config, rule)
            for item in rule_items:
                if dates and item.date not in dates:
                    continue
                items.append(item)
        return sorted(items, key=lambda item: (item.date, item.rule, item.relative_path))

    def sync(
        self,
        *,
        dates: set[str] | None = None,
        rule_names: set[str] | None = None,
        dry_run: bool = False,
        workers: int = 4,
    ) -> SyncSummary:
        if workers < 1:
            raise ValueError("workers must be at least 1")

        if workers == 1:
            return self._sync_serial(
                dates=dates,
                rule_names=rule_names,
                dry_run=dry_run,
            )

        items = self.discover(dates=dates, rule_names=rule_names)
        counters = defaultdict(int)
        counters["discovered"] = len(items)
        folder_cache: dict[str, dict[str, dict]] = {}
        pending_uploads: list[LocalSyncItem] = []

        for item in items:
            record = self.inventory.get(
                item.region,
                item.rule,
                item.date,
                item.relative_path,
            )
            if record and record.status == "uploaded" and _same_signature(record, item):
                counters["skipped_inventory"] += 1
                continue

            remote_files = self._remote_files(item.drive_path, folder_cache, create=not dry_run)
            remote = remote_files.get(item.file_name)
            if remote:
                self._record_uploaded(item, remote)
                counters["skipped_remote"] += 1
                continue

            if dry_run:
                logger.info("Would upload %s to %s", item.local_path, item.drive_path)
                continue

            pending_uploads.append(item)

        self._upload_parallel(pending_uploads, workers, counters)
        return SyncSummary(**counters)

    def _sync_serial(
        self,
        *,
        dates: set[str] | None,
        rule_names: set[str] | None,
        dry_run: bool,
    ) -> SyncSummary:
        items = self.discover(dates=dates, rule_names=rule_names)
        counters = defaultdict(int)
        counters["discovered"] = len(items)
        folder_cache: dict[str, dict[str, dict]] = {}

        for item in items:
            record = self.inventory.get(
                item.region,
                item.rule,
                item.date,
                item.relative_path,
            )
            if record and record.status == "uploaded" and _same_signature(record, item):
                counters["skipped_inventory"] += 1
                continue

            remote_files = self._remote_files(item.drive_path, folder_cache, create=not dry_run)
            remote = remote_files.get(item.file_name)
            if remote:
                self._record_uploaded(item, remote)
                counters["skipped_remote"] += 1
                continue

            if dry_run:
                logger.info("Would upload %s to %s", item.local_path, item.drive_path)
                continue

            try:
                uploaded = self.drive.upload_file(item.local_path, item.drive_path)
                self._record_uploaded(item, uploaded)
                folder_cache.pop(item.drive_path, None)
                counters["uploaded"] += 1
            except Exception:
                logger.exception("Failed to upload %s", item.local_path)
                counters["errors"] += 1

        return SyncSummary(**counters)

    def _upload_parallel(
        self,
        items: list[LocalSyncItem],
        workers: int,
        counters,
    ) -> None:
        if not items:
            return

        thread_state = threading.local()

        def client_for_thread():
            drive_client = getattr(thread_state, "drive_client", None)
            if drive_client is None:
                new_worker_client = getattr(self.drive, "new_worker_client", None)
                drive_client = new_worker_client() if new_worker_client else self.drive
                thread_state.drive_client = drive_client
            return drive_client

        def upload(item: LocalSyncItem):
            uploaded = client_for_thread().upload_file(item.local_path, item.drive_path)
            return item, uploaded

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(upload, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    uploaded_item, uploaded = future.result()
                    self._record_uploaded(uploaded_item, uploaded)
                    counters["uploaded"] += 1
                except Exception:
                    logger.exception("Failed to upload %s", item.local_path)
                    counters["errors"] += 1

    def reconcile(
        self,
        *,
        dates: set[str] | None = None,
        rule_names: set[str] | None = None,
        repair_mode: str = "report",
    ) -> SyncSummary:
        if repair_mode not in {"report", "upload-missing"}:
            raise ValueError("repair_mode must be 'report' or 'upload-missing'")

        items = self.discover(dates=dates, rule_names=rule_names)
        counters = defaultdict(int)
        counters["discovered"] = len(items)
        folder_cache: dict[str, dict[str, dict]] = {}
        expected_remote_keys: set[tuple[str, str]] = set()

        for item in items:
            expected_remote_keys.add((item.drive_path, item.file_name))
            record = self.inventory.get(
                item.region,
                item.rule,
                item.date,
                item.relative_path,
            )
            if record and record.status == "uploaded" and not _same_signature(record, item):
                counters["changed_local"] += 1
                logger.warning("Local file changed since inventory: %s", item.local_path)

            remote_files = self._remote_files(item.drive_path, folder_cache, create=False)
            remote = remote_files.get(item.file_name)
            if remote:
                self._record_uploaded(item, remote)
            else:
                counters["missing_remote"] += 1
                logger.warning("Missing remote file for %s -> %s", item.local_path, item.drive_path)
                if repair_mode == "upload-missing":
                    uploaded = self.drive.upload_file(item.local_path, item.drive_path)
                    self._record_uploaded(item, uploaded)
                    counters["uploaded"] += 1

        for drive_path, remote_files in folder_cache.items():
            for file_name, remote in remote_files.items():
                if (drive_path, file_name) in expected_remote_keys:
                    continue
                date = _extract_date_from_path(drive_path) or "unknown"
                relative_path = f"{drive_path.rstrip('/')}/{file_name}"
                self.inventory.mark_remote_only(
                    region=self.config.region,
                    rule="unknown",
                    date=date,
                    relative_path=relative_path,
                    drive_path=drive_path,
                    file_name=file_name,
                    drive_file_id=remote.get("id"),
                    drive_modified_time=remote.get("modifiedTime"),
                )
                counters["remote_only"] += 1
                logger.warning("Remote-only file in Drive: %s/%s", drive_path, file_name)

        return SyncSummary(**counters)

    def list_drive(self, *, date: str | None = None) -> list[dict]:
        root = self.config.drive_root
        drive_path = f"{root}/{date}" if date else root
        return self.drive.list_tree(drive_path)

    def _remote_files(
        self,
        drive_path: str,
        folder_cache: dict[str, dict[str, dict]],
        *,
        create: bool,
    ) -> dict[str, dict]:
        if drive_path not in folder_cache:
            folder_cache[drive_path] = self.drive.list_files(drive_path, create=create)
        return folder_cache[drive_path]

    def _record_uploaded(self, item: LocalSyncItem, remote: dict | None) -> None:
        self.inventory.upsert(
            InventoryRecord(
                region=item.region,
                rule=item.rule,
                date=item.date,
                relative_path=item.relative_path,
                local_path=str(item.local_path),
                drive_path=item.drive_path,
                file_name=item.file_name,
                status="uploaded",
                size=item.size,
                mtime_ns=item.mtime_ns,
                drive_file_id=(remote or {}).get("id"),
                drive_modified_time=(remote or {}).get("modifiedTime"),
            )
        )


def _discover_rule(config: SyncConfig, rule: SyncRule) -> list[LocalSyncItem]:
    if rule.kind == "files":
        return _discover_files_rule(config, rule)
    if rule.kind == "dated_directory":
        return _discover_dated_directory_rule(config, rule)
    raise ValueError(f"Unsupported sync rule kind {rule.kind!r} for {rule.name!r}")


def _discover_files_rule(config: SyncConfig, rule: SyncRule) -> list[LocalSyncItem]:
    local_root = config.sync_root / _render(rule.local_root, config, date="")
    if not local_root.exists():
        logger.debug("Skipping missing sync root for rule %s: %s", rule.name, local_root)
        return []

    date_pattern = re.compile(rule.date_regex)
    dates = sorted(
        {
            match.group(0)
            for file_path in local_root.rglob("*")
            if file_path.is_file()
            if not _ignored(file_path, local_root, rule)
            for match in [date_pattern.search(file_path.as_posix())]
            if match
        }
    )
    items: list[LocalSyncItem] = []
    for date in dates:
        for pattern in rule.patterns:
            local_path = local_root / pattern.format(date=date)
            if local_path.is_file() and not _ignored(local_path, local_root, rule):
                items.append(_make_item(config, rule, date, local_path, local_path.relative_to(local_root)))
    return items


def _discover_dated_directory_rule(config: SyncConfig, rule: SyncRule) -> list[LocalSyncItem]:
    local_root = config.sync_root / _render(rule.local_root, config, date="")
    if not local_root.exists():
        logger.debug("Skipping missing sync root for rule %s: %s", rule.name, local_root)
        return []

    date_pattern = re.compile(f"^{rule.date_regex}$")
    items: list[LocalSyncItem] = []
    for date_dir in sorted(path for path in local_root.iterdir() if path.is_dir()):
        date = date_dir.name
        if not date_pattern.match(date):
            continue
        for local_path in sorted(
            path
            for path in date_dir.rglob("*")
            if path.is_file() and not _ignored(path, local_root, rule)
        ):
            relative = local_path.relative_to(date_dir)
            items.append(_make_item(config, rule, date, local_path, relative))
    return items


def _make_item(
    config: SyncConfig,
    rule: SyncRule,
    date: str,
    local_path: Path,
    relative_path: Path,
) -> LocalSyncItem:
    stat = local_path.stat()
    relative_posix = relative_path.as_posix()
    drive_path = _render(rule.drive_path, config, date=date)
    if rule.kind == "dated_directory" and relative_path.parent != Path("."):
        drive_path = f"{drive_path.rstrip('/')}/{relative_path.parent.as_posix()}"
    return LocalSyncItem(
        region=config.region,
        rule=rule.name,
        date=date,
        local_path=local_path,
        relative_path=relative_posix,
        drive_path=drive_path,
        file_name=local_path.name,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _same_signature(record: InventoryRecord, item: LocalSyncItem) -> bool:
    return record.size == item.size and record.mtime_ns == item.mtime_ns


def _extract_date_from_path(path: str) -> str | None:
    match = re.search(r"\d{8}T\d{2}|\d{8}", path)
    return match.group(0) if match else None


def _render(value: str, config: SyncConfig, *, date: str) -> str:
    return value.format(
        drive_root=config.drive_root,
        cluster=config.cluster,
        region=config.region,
        date=date,
    )


def _ignored(path: Path, local_root: Path, rule: SyncRule) -> bool:
    relative = path.relative_to(local_root).as_posix()
    return any(
        fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern)
        for pattern in rule.ignore_patterns
    )
