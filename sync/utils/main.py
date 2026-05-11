from __future__ import annotations

from pathlib import Path
from datetime import datetime
import argparse
import json
import logging
import shutil
import subprocess

try:
    from .drive import GoogleDriveClient
    from .sync_config import default_project_root, load_sync_configs
    from .sync_engine import SyncEngine
    from .sync_inventory import SyncInventory
except ImportError:  # pragma: no cover - supports python sync/utils/main.py
    from drive import GoogleDriveClient
    from sync_config import default_project_root, load_sync_configs
    from sync_engine import SyncEngine
    from sync_inventory import SyncInventory


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)
logger = logging.getLogger(__name__)


def parse_forecast_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y%m%dT%H")


def most_recent_date(date_list: list[str]) -> str | None:
    valid_dates = []
    for date_str in date_list:
        try:
            parse_forecast_date(date_str)
        except ValueError:
            continue
        valid_dates.append(date_str)
    return max(valid_dates) if valid_dates else None


def load_runtime_config(project_root: Path) -> dict:
    config_file = project_root / ".config" / "config.json"
    if not config_file.exists():
        return {}
    with config_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def update_live_assets(project_root: Path) -> None:
    logger.info(
        "Live asset update is disabled; skipping monsoon-operational update."
    )
    return

    # Disabled for now: this block updates and pushes the sibling
    # monsoon-operational repository from sync/latest outputs.
    operational_dir = project_root.parent / "monsoon-operational"
    live_dir = operational_dir / "docs" / "assets"
    maps_dir = live_dir / "images"
    data_dir = live_dir / "data"
    latest_root = project_root / "sync" / "latest"

    if not latest_root.exists():
        logger.info("No sync/latest directory found; skipping live asset update.")
        return

    date = most_recent_date([path.name for path in latest_root.iterdir() if path.is_dir()])
    if not date:
        logger.info("No dated folders found in sync/latest; skipping live asset update.")
        return

    latest_dir = latest_root / date
    live_date_ref = data_dir / "latest.txt"
    live_date = live_date_ref.read_text(encoding="utf-8").strip() if live_date_ref.exists() else ""
    if live_date:
        try:
            if parse_forecast_date(date) <= parse_forecast_date(live_date):
                logger.info("Live assets are already current: live=%s latest=%s", live_date, date)
                return
        except ValueError:
            logger.warning("Invalid live date %r; replacing with %s", live_date, date)

    runtime_config = load_runtime_config(project_root)
    cluster_id = runtime_config.get("cluster_id", runtime_config.get("cluster", "unknown"))

    _run_git(operational_dir, "pull")
    _replace_live_files(latest_dir, maps_dir, data_dir)
    live_date_ref.write_text(date, encoding="utf-8")
    (data_dir / "cluster.txt").write_text(str(cluster_id), encoding="utf-8")
    _run_git(operational_dir, "add", ".")
    _run_git(operational_dir, "commit", "-m", f"Updated live date to {date}", allow_failure=True)
    _run_git(operational_dir, "push")
    logger.info("Updated live assets to %s", date)


def _replace_live_files(latest_dir: Path, maps_dir: Path, data_dir: Path) -> None:
    maps_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    for path in maps_dir.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    for path in data_dir.iterdir():
        if path.name in {"latest.txt", "cluster.txt"}:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    map_files = sorted((latest_dir / "maps").glob("map_bars_with_probs_country_*.png"))
    if map_files:
        copied_map = maps_dir / map_files[0].name
        shutil.copy2(map_files[0], copied_map)
        suffix = "_" + copied_map.stem.split("_")[-1]
        renamed = copied_map.with_name(copied_map.stem.replace(suffix, "") + copied_map.suffix)
        copied_map.rename(renamed)
        logger.info("Copied live map %s", renamed)
    else:
        logger.warning("No live map found under %s", latest_dir / "maps")

    messages = latest_dir / "messages" / "message_templates_output_eng.csv"
    if messages.exists():
        shutil.copy2(messages, data_dir / messages.name)
    else:
        logger.warning("No message template found at %s", messages)


def _run_git(repo: Path, *args: str, allow_failure: bool = False) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    if result.returncode != 0:
        logger.info("git %s skipped: %s", " ".join(args), result.stderr.strip())


def run_drive_action(args) -> None:
    configs = load_sync_configs(
        args.config,
        sync_root=args.sync_root,
        cluster=args.cluster,
        drive_root=args.drive_root,
        inventory_path=args.inventory,
        regions=args.region,
    )
    dates = set(args.date) if args.date else None
    rules = set(args.rule) if args.rule else None
    drive_client = GoogleDriveClient.authenticated()
    for config in configs:
        with SyncInventory(config.inventory_path) as inventory:
            engine = SyncEngine(config, drive_client, inventory)
            logger.info("Running %s for region=%s drive_root=%s", args.action, config.region, config.drive_root)
            if args.action == "sync":
                summary = engine.sync(
                    dates=dates,
                    rule_names=rules,
                    dry_run=args.dry_run,
                    workers=args.workers,
                )
            elif args.action == "reconcile":
                summary = engine.reconcile(
                    dates=dates,
                    rule_names=rules,
                    repair_mode=args.repair_mode,
                )
            else:
                for item in engine.list_drive(date=args.date[0] if args.date else None):
                    logger.info("%s", item.get("path"))
                continue
        logger.info("%s summary for region=%s: %s", args.action, config.region, summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monsoon operational sync")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["sync", "reconcile", "ls-drive", "live"],
        default="sync",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--sync-root", type=Path, default=None)
    parser.add_argument("--cluster", type=str, default=None)
    parser.add_argument("--region", type=str, nargs="+", default=None)
    parser.add_argument("--drive-root", type=str, default=None)
    parser.add_argument("--inventory", type=Path, default=None)
    parser.add_argument("--date", type=str, nargs="+", default=None)
    parser.add_argument("--rule", type=str, nargs="+", default=None)
    parser.add_argument("--repair-mode", choices=["report", "upload-missing"], default="report")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel upload workers for sync actions. Use 1 for serial uploads.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Skip live repository asset updates before the default sync action.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")

    project_root = default_project_root()

    if args.action == "live":
        update_live_assets(project_root)
        return

    if args.action == "sync" and not args.skip_live:
        try:
            update_live_assets(project_root)
        except Exception:
            logger.exception("Live asset update failed; continuing with Drive sync.")

    run_drive_action(args)


if __name__ == "__main__":
    main()
