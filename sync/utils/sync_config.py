from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
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
    ignore_patterns: tuple[str, ...] = (".nfs*",)


@dataclass(frozen=True)
class SyncConfig:
    sync_root: Path
    drive_root: str
    inventory_path: Path
    rules: tuple[SyncRule, ...]
    cluster: str
    region: str


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


def load_sync_configs(
    config_path: Path | str | None = None,
    *,
    sync_root: Path | str | None = None,
    cluster: str | None = None,
    drive_root: str | None = None,
    inventory_path: Path | str | None = None,
    regions: str | Iterable[str] | None = None,
) -> tuple[SyncConfig, ...]:
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
    all_region_data = data.get("regions")
    if not isinstance(all_region_data, dict) or not all_region_data:
        raise ValueError(f"Sync config must define a non-empty regions mapping: {selected_config}")

    selected_regions = _selected_regions(regions, all_region_data)
    inventory_template = (
        inventory_path
        or os.environ.get("MONSOON_SYNC_DB")
        or data.get("inventory", {}).get("path")
        or "sync/state/drive_inventory_{region}_{cluster}.sqlite3"
    )

    configs: list[SyncConfig] = []
    for region in selected_regions:
        region_data = all_region_data[region]
        if not isinstance(region_data, dict):
            raise ValueError(f"Region {region!r} must be a mapping")

        selected_drive_root = (
            drive_root
            or os.environ.get("MONSOON_DRIVE_ROOT")
            or region_data.get("drive", {}).get("root")
            or data.get("drive", {}).get("root")
        )
        if not selected_drive_root:
            raise ValueError(f"Region {region!r} does not define a Drive root")
        selected_drive_root = _format_template(
            selected_drive_root,
            region=region,
            cluster=selected_cluster,
            drive_root="",
        )

        rules = tuple(_parse_rule(raw) for raw in region_data.get("rules", []))
        if not rules:
            raise ValueError(f"Region {region!r} has no sync rules configured")

        formatted_inventory = (
            inventory_template
            if isinstance(inventory_template, Path)
            else _format_template(
                str(inventory_template),
                region=region,
                cluster=selected_cluster,
                drive_root=selected_drive_root,
            )
        )
        selected_inventory = Path(formatted_inventory).expanduser()
        if not selected_inventory.is_absolute():
            selected_inventory = selected_root / selected_inventory

        configs.append(
            SyncConfig(
                sync_root=selected_root,
                drive_root=selected_drive_root,
                inventory_path=selected_inventory,
                rules=rules,
                cluster=selected_cluster,
                region=region,
            )
        )

    return tuple(configs)


def load_sync_config(
    config_path: Path | str | None = None,
    *,
    sync_root: Path | str | None = None,
    cluster: str | None = None,
    drive_root: str | None = None,
    inventory_path: Path | str | None = None,
    region: str | None = None,
) -> SyncConfig:
    configs = load_sync_configs(
        config_path,
        sync_root=sync_root,
        cluster=cluster,
        drive_root=drive_root,
        inventory_path=inventory_path,
        regions=region,
    )
    if len(configs) != 1:
        raise ValueError("load_sync_config requires exactly one selected region")
    return configs[0]


def _selected_regions(
    regions: str | Iterable[str] | None,
    all_region_data: dict[str, Any],
) -> tuple[str, ...]:
    requested = regions or os.environ.get("MONSOON_REGION") or os.environ.get("FORECAST_REGION")
    if requested is None:
        return tuple(all_region_data.keys())

    if isinstance(requested, str):
        names = [name.strip() for name in requested.split(",") if name.strip()]
    else:
        names = [
            region_name
            for name in requested
            for region_name in str(name).split(",")
            if region_name.strip()
        ]
        names = [name.strip() for name in names]

    missing = [name for name in names if name not in all_region_data]
    if missing:
        raise ValueError(f"Unknown sync region(s): {', '.join(missing)}")
    return tuple(names)


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
        ignore_patterns=tuple(raw.get("ignore_patterns") or (".nfs*",)),
    )


def _format_template(value: str, **kwargs: str) -> str:
    return value.format(**kwargs)


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
