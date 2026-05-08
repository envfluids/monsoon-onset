from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import os


@dataclass(frozen=True)
class SyncRule:
    name: str
    kind: str
    local_root: str
    drive_path: str
    date_regex: str
    patterns: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SyncConfig:
    sync_root: Path
    drive_root: str
    inventory_path: Path
    rules: tuple[SyncRule, ...]
    cluster: str


def default_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def default_config_path(project_root: Path | None = None) -> Path:
    root = project_root or default_project_root()
    return root / "sync" / "config" / "sync.yaml"


def load_cluster(project_root: Path | None = None, fallback: str = "local") -> str:
    root = project_root or default_project_root()
    config_file = root / ".config" / "config.json"
    if not config_file.exists():
        return fallback
    with config_file.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data.get("cluster") or fallback


def load_sync_config(
    config_path: Path | str | None = None,
    *,
    sync_root: Path | str | None = None,
    cluster: str | None = None,
    drive_root: str | None = None,
    inventory_path: Path | str | None = None,
) -> SyncConfig:
    project_root = default_project_root()
    selected_config = Path(config_path) if config_path else default_config_path(project_root)
    data = _load_yaml(selected_config)

    selected_root = Path(
        sync_root
        or os.environ.get("MONSOON_SYNC_ROOT")
        or data.get("sync_root")
        or project_root
    ).expanduser()
    if not selected_root.is_absolute():
        selected_root = (project_root / selected_root).resolve()

    selected_cluster = cluster or os.environ.get("MONSOON_CLUSTER") or load_cluster(project_root)
    selected_drive_root = (
        drive_root
        or os.environ.get("MONSOON_DRIVE_ROOT")
        or data.get("drive", {}).get("root")
        or "/MO_operational_data_2026/{cluster}"
    ).format(cluster=selected_cluster)

    inventory_value = (
        inventory_path
        or os.environ.get("MONSOON_SYNC_DB")
        or data.get("inventory", {}).get("path")
        or "sync/state/drive_inventory_{cluster}.sqlite3"
    )
    if isinstance(inventory_value, Path):
        formatted_inventory = inventory_value
    else:
        formatted_inventory = inventory_value.format(
            cluster=selected_cluster,
        )
    selected_inventory = Path(formatted_inventory).expanduser()
    if not selected_inventory.is_absolute():
        selected_inventory = selected_root / selected_inventory

    rules = tuple(_parse_rule(raw) for raw in data.get("rules", []))
    if not rules:
        raise ValueError(f"No sync rules configured in {selected_config}")

    return SyncConfig(
        sync_root=selected_root,
        drive_root=selected_drive_root,
        inventory_path=selected_inventory,
        rules=rules,
        cluster=selected_cluster,
    )


def _parse_rule(raw: dict[str, Any]) -> SyncRule:
    required = ["name", "kind", "local_root", "drive_path", "date_regex"]
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Sync rule is missing required keys: {missing}")
    patterns = tuple(raw.get("patterns") or ())
    if raw["kind"] == "files" and not patterns:
        raise ValueError(f"File sync rule {raw['name']!r} must define patterns")
    return SyncRule(
        name=str(raw["name"]),
        kind=str(raw["kind"]),
        local_root=str(raw["local_root"]),
        drive_path=str(raw["drive_path"]),
        date_regex=str(raw["date_regex"]),
        patterns=patterns,
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Sync config not found: {path}")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load sync YAML config") from exc
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Sync config must be a mapping: {path}")
    return loaded
