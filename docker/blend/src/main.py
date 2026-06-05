"""
Monsoon Blend — GCS Shim Wrapper

Downloads per-region model post-processed outputs from the REGION bucket and
blend support files from the COMMON bucket, runs the configured blend through
blend/utils/main.py, then uploads blend outputs back to the region bucket.

Environment Variables:
    DATE              : Blend forecast date YYYYMMDDTHH
    FORECAST_REGION   : Region whose blend to run
    RUN_MODE          : all, blend, or diagnostics (default: all)
    GCS_COMMON_BUCKET : Common bucket (blend supports under weights/blend/{region}/...)
    GCS_REGION_BUCKETS: JSON map {region: bucket} for model outputs and blend output
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import click
from google.cloud import storage

from blend.utils.main import BLENDS, BlendConfig, ForecastInput

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

REPO_ROOT   = Path("/app")
BLEND_UTILS = REPO_ROOT / "blend" / "utils"
REGIONS = json.loads(os.environ.get("REGIONS", "{}"))

LOCAL_BLEND_OUT_BASE = REPO_ROOT / "blend" / "output"
LOCAL_DIAGNOSTICS_OUT_BASE = REPO_ROOT / "model_diagnostics" / "output"
LOCAL_BLEND_DATA = REPO_ROOT / "blend" / "data"
COMMON_BUCKET_MOUNT = Path("/mnt/disks/common")

BLEND_MODEL_TO_PIPELINE_MODEL = {
    "AIFS_SINGLE_V2": "AIFS_single_v2",
    "AIFS_ENS_V2": "AIFS_ENS_v2",
    "GENCAST": "gencast",
    "NCUM": "ncum",
    "NGCM": "neuralgcm",
    "NEURALGCM": "neuralgcm",
}

FULL_FIELD_MODELS = {
    "AIFS_SINGLE_V2": {"prefix": "AIFS_single_v2", "kind": "file", "suffix": ".nc"},
    "AIFS_ENS_V2": {"prefix": "AIFS_ENS_v2", "kind": "directory", "suffix": ".zarr"},
    "GENCAST": {"prefix": "gencast", "kind": "directory", "suffix": ".zarr"},
    "NEURALGCM": {"prefix": "neuralgcm", "kind": "directory", "suffix": ".zarr"},
    "NGCM": {"prefix": "neuralgcm", "kind": "directory", "suffix": ".zarr"},
}


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def gcs_object_exists(bucket_name: str, gcs_path: str) -> bool:
    return _client().bucket(bucket_name).blob(gcs_path).exists()


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info("Wrote gs://%s/%s", bucket_name, gcs_path)


def upload_gcs_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info("Uploaded %s → gs://%s/%s", local_path, bucket_name, gcs_path)


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
        logger.info("Downloaded gs://%s/%s → %s", bucket_name, blob.name, local_path)
        count += 1
    if count == 0:
        logger.info("No objects found at gs://%s/%s", bucket_name, gcs_prefix)
    return count


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    if not local_dir.exists():
        logger.info("Blend output directory does not exist, skipping upload: %s", local_dir)
        return
    bucket = _client().bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info("Uploaded %s → gs://%s/%s", local_file, bucket_name, gcs_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",   envvar="DATE",            required=True)
@click.option("--region", envvar="FORECAST_REGION", required=True)
@click.option("--run-mode", envvar="RUN_MODE", default="all",
              type=click.Choice(["all", "blend", "diagnostics"]))
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True,
              help="JSON map {region: bucket}")
@click.option("--blend-names", envvar="BLEND_NAMES", default="")
def main(date, region, run_mode, common_bucket, region_buckets, blend_names):
    region_buckets = json.loads(region_buckets)
    if region not in region_buckets:
        raise click.ClickException(f"No bucket configured for region {region!r}")
    region_bucket = region_buckets[region]

    logger.info("Blend shim: region=%s date=%s run_mode=%s", region, date, run_mode)

    selected_names = _parse_blend_names(blend_names)
    blends = _select_blends(region, run_mode, selected_names)

    _setup_directories(blends, date, run_mode)
    _download_cached_climatologies(common_bucket, region, run_mode, blends)
    ready_blends = _download_inputs(date, region, region_bucket, common_bucket, blends, run_mode)
    if not ready_blends:
        logger.info("No selected %s configs are ready for %s/%s", run_mode, region, date)
        return
    ran_blends = _run_blend(date, region, run_mode, ready_blends)
    _upload_cached_climatologies(common_bucket, region, run_mode, ran_blends)
    _upload_outputs(date, region, region_bucket, common_bucket, run_mode, ran_blends)


def _pipeline_models_for_blend(blend: BlendConfig) -> set[str]:
    return {
        BLEND_MODEL_TO_PIPELINE_MODEL.get(model.upper(), model)
        for model in blend.models()
    }


ETHIOPIA_BLEND_CLIMATOLOGY_FILES = (
    "imd_clim_mok_date_clim_issue.pkl",
    "imd_clim_mok_date_clim_unc_issue.pkl",
)
ETHIOPIA_BLEND_ONSET_DEFINITIONS_BY_NAME = {
    "AIFS_single_v1p1_AIFS_ENS_v1": ("ICPAC",),
    "AIFS_single_v2_AIFS_ENS_v2": ("ICPAC", "2mm"),
    "AIFS_single_v2_NeuralGCM": ("ICPAC",),
}
ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR = (
    REPO_ROOT
    / "model_diagnostics"
    / "data"
    / "climatology"
    / "era5_clim_africa_1990-2019.zarr"
)
CACHE_PREFIX = "cache"


def _repo_relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _cache_prefix_for_path(path: Path) -> str:
    return f"{CACHE_PREFIX}/{_repo_relative(path)}"


def _ethiopia_blend_climatology_dirs(blends: list[BlendConfig]) -> list[Path]:
    dirs = []
    for blend in blends:
        onset_definitions = ETHIOPIA_BLEND_ONSET_DEFINITIONS_BY_NAME.get(blend.name, ())
        for onset_definition in onset_definitions:
            dirs.append(
                REPO_ROOT
                / "blend"
                / "utils"
                / "ethiopia2026"
                / "operational"
                / "Monsoon_Data"
                / "Processed_Data"
                / f"{blend.name}_{onset_definition}"
            )
    return dirs


def _download_cached_climatologies(
    common_bucket: str,
    region: str,
    run_mode: str,
    blends: list[BlendConfig],
) -> None:
    if region != "ethiopia":
        return
    if run_mode in {"all", "blend"}:
        for clim_dir in _ethiopia_blend_climatology_dirs(blends):
            clim_dir.mkdir(parents=True, exist_ok=True)
            for filename in ETHIOPIA_BLEND_CLIMATOLOGY_FILES:
                local_path = clim_dir / filename
                if local_path.exists():
                    continue
                gcs_path = f"{_cache_prefix_for_path(clim_dir)}/{filename}"
                if gcs_object_exists(common_bucket, gcs_path):
                    download_gcs_file(common_bucket, gcs_path, local_path)
    if run_mode in {"all", "diagnostics"} and not _path_has_files(ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR):
        downloaded = download_gcs_prefix(
            common_bucket,
            f"{_cache_prefix_for_path(ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR)}/",
            ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR,
        )
        if downloaded:
            logger.info(
                "Restored Ethiopia diagnostics climatology from gs://%s/%s/",
                common_bucket,
                _cache_prefix_for_path(ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR),
            )


def _upload_cached_climatologies(
    common_bucket: str,
    region: str,
    run_mode: str,
    blends: list[BlendConfig],
) -> None:
    if region != "ethiopia":
        return
    if run_mode in {"all", "blend"}:
        for clim_dir in _ethiopia_blend_climatology_dirs(blends):
            for filename in ETHIOPIA_BLEND_CLIMATOLOGY_FILES:
                local_path = clim_dir / filename
                if not local_path.exists():
                    continue
                upload_gcs_file(
                    common_bucket,
                    local_path,
                    f"{_cache_prefix_for_path(clim_dir)}/{filename}",
                )
    if run_mode in {"all", "diagnostics"}:
        upload_directory(
            common_bucket,
            ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR,
            _cache_prefix_for_path(ETHIOPIA_DIAGNOSTICS_CLIMATOLOGY_DIR),
        )


def _parse_blend_names(raw: str) -> set[str]:
    if not raw:
        return set()
    stripped = raw.strip()
    if not stripped:
        return set()
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise click.ClickException("BLEND_NAMES JSON must be a list of strings")
        return set(parsed)
    return {item.strip() for item in stripped.split(",") if item.strip()}


def _select_blends(region: str, run_mode: str, selected_names: set[str]) -> list[BlendConfig]:
    configured_models = set(REGIONS.get(region, {}).get("models", []))
    blends = [
        blend for blend in BLENDS
        if blend.region == region
        and (not selected_names or blend.name in selected_names)
        and (
            (run_mode in {"all", "blend"} and blend.blend_implemented)
            or (run_mode in {"all", "diagnostics"} and blend.diagnostic_plots)
        )
        and (not configured_models or _pipeline_models_for_blend(blend).issubset(configured_models))
    ]
    matched = {blend.name for blend in blends}
    missing = selected_names - matched
    if missing:
        raise click.ClickException(
            f"Selected {run_mode} configuration(s) are not available for {region}: {sorted(missing)}"
        )
    if not blends:
        raise click.ClickException(
            f"No {run_mode} configuration matched region {region!r}"
        )
    return blends


def _inputs_for_mode(blend: BlendConfig, run_mode: str) -> tuple[ForecastInput, ...]:
    inputs = list(blend.inputs) if run_mode in {"all", "blend"} else []
    if run_mode in {"all", "diagnostics"}:
        inputs.extend(blend.diagnostic_inputs or blend.inputs)
    unique = {}
    for input_ in inputs:
        unique[(input_.model, input_.role, input_.path_template)] = input_
    return tuple(unique.values())


def _setup_directories(blends: list[BlendConfig], date: str, run_mode: str) -> None:
    LOCAL_BLEND_OUT_BASE.mkdir(parents=True, exist_ok=True)
    LOCAL_DIAGNOSTICS_OUT_BASE.mkdir(parents=True, exist_ok=True)
    LOCAL_BLEND_DATA.mkdir(parents=True, exist_ok=True)
    for blend in blends:
        for input_ in _inputs_for_mode(blend, run_mode):
            input_.path(date).parent.mkdir(parents=True, exist_ok=True)
        if run_mode in {"all", "diagnostics"}:
            for model in blend.models():
                if _full_field_config_for_model(model):
                    _raw_full_field_path(model, date).parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )


def _download_inputs(
    date: str,
    region: str,
    region_bucket: str,
    common_bucket: str,
    blends: list[BlendConfig],
    run_mode: str,
) -> list[BlendConfig]:
    ready_blends = []
    for blend in blends:
        missing = [
            f"gs://{region_bucket}/{_blend_input_bucket_path(region, input_, date)}"
            for input_ in _inputs_for_mode(blend, run_mode)
            if not gcs_object_exists(
                region_bucket,
                _blend_input_bucket_path(region, input_, date),
            )
        ]
        if missing:
            logger.warning(
                "Skipping %s/%s for %s; missing selected input(s): %s",
                region,
                blend.name,
                date,
                ", ".join(missing),
            )
            continue
        for input_ in _inputs_for_mode(blend, run_mode):
            download_gcs_file(
                region_bucket,
                _blend_input_bucket_path(region, input_, date),
                input_.path(date),
            )
        if run_mode in {"all", "diagnostics"}:
            try:
                _download_raw_full_fields(common_bucket, blend, date)
            except click.ClickException as exc:
                logger.warning(
                    "Skipping %s/%s diagnostics for %s; %s",
                    region,
                    blend.name,
                    date,
                    exc,
                )
                continue
        ready_blends.append(blend)

    if run_mode == "diagnostics" or not any(blend.blend_implemented for blend in ready_blends):
        return ready_blends

    # Blend support/coefficient files are gitignored when too large or
    # environment-specific. Preserve the object layout below weights/blend/{region}
    # under /app/blend/data so science scripts can use their repo-local paths.
    downloaded = download_gcs_prefix(
        common_bucket,
        f"weights/blend/{region}/",
        LOCAL_BLEND_DATA,
    )
    if downloaded == 0:
        logger.warning(
            "No blend support files found at gs://%s/weights/blend/%s/ — "
            "blend will fail if it needs these files",
            common_bucket, region,
        )
    return ready_blends


def _pipeline_model_for_blend_input(input_: ForecastInput) -> str:
    return BLEND_MODEL_TO_PIPELINE_MODEL.get(input_.model.upper(), input_.model)


def _full_field_config_for_model(model: str) -> dict | None:
    return FULL_FIELD_MODELS.get(model.upper())


def _raw_full_field_path(model: str, date: str) -> Path:
    config = _full_field_config_for_model(model)
    if not config:
        raise click.ClickException(f"No full-field mapping is configured for {model}")
    if config["prefix"] == "neuralgcm":
        return REPO_ROOT / "NeuralGCM" / "output" / "raw" / f"{date}.zarr"
    if config["prefix"] == "gencast":
        return REPO_ROOT / "gencast" / "raw" / "output" / f"init_{date}.zarr"
    return (
        REPO_ROOT
        / "AIFS"
        / "output"
        / "raw"
        / model
        / f"init_{date}{config['suffix']}"
    )


def _download_raw_full_fields(common_bucket: str, blend: BlendConfig, date: str) -> None:
    for model in sorted(blend.models()):
        config = _full_field_config_for_model(model)
        if config is None:
            continue
        local_path = _raw_full_field_path(model, date)
        mounted_path = _mounted_full_field_path(config, date)
        if mounted_path.exists():
            _symlink_full_field(mounted_path, local_path)
            continue

        gcs_prefix = f"full_field/{config['prefix']}/{date}/"
        if config["kind"] == "directory":
            source_prefix = gcs_prefix
            if config["prefix"] == "neuralgcm":
                source_prefix = f"full_field/neuralgcm/{date}.zarr/"
            else:
                source_prefix = f"{gcs_prefix}init_{date}{config['suffix']}/"
            downloaded = download_gcs_prefix(common_bucket, source_prefix, local_path)
            if downloaded == 0:
                raise click.ClickException(
                    f"No {model} full-field objects found at "
                    f"gs://{common_bucket}/{source_prefix}"
                )
            continue
        download_gcs_file(
            common_bucket,
            f"{gcs_prefix}init_{date}{config['suffix']}",
            local_path,
        )


def _mounted_full_field_path(config: dict, date: str) -> Path:
    base = COMMON_BUCKET_MOUNT / "full_field" / config["prefix"] / date
    if config["prefix"] == "neuralgcm":
        return base.with_suffix(".zarr")
    if config["kind"] == "directory":
        return base / f"init_{date}{config['suffix']}"
    if config["kind"] == "file":
        return base / f"init_{date}{config['suffix']}"
    return base


def _symlink_full_field(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        return
    target.symlink_to(source, target_is_directory=source.is_dir())
    logger.info("Using mounted full-field input %s -> %s", target, source)


def _blend_input_bucket_path(region: str, input_: ForecastInput, date: str) -> str:
    model = _pipeline_model_for_blend_input(input_)
    if model == "gencast":
        return f"output/gencast/{date}/tp_0p25_{date}.nc"

    parts = PurePosixPath(input_.path_template.format(date=date)).parts
    if len(parts) < 3 or parts[1] != "output":
        raise click.ClickException(
            f"Unsupported blend input path for {input_.model}: {input_.path_template}"
        )

    if len(parts) >= 4 and parts[2] == region:
        relative_parts = parts[3:]
    else:
        relative_parts = parts[2:]

    return "/".join(("output", model, date, *relative_parts))


def _run_blend(
    date: str,
    region: str,
    run_mode: str,
    blends: list[BlendConfig],
) -> list[BlendConfig]:
    env = {**os.environ, "PYTHONPATH": str(BLEND_UTILS)}
    ran_blends = []
    for blend in blends:
        command = [
            sys.executable,
            "main.py",
            "--date",
            date,
            "--region",
            region,
            "--blend",
            blend.name,
        ]
        if run_mode == "blend":
            command.append("--blend-only")
        elif run_mode == "diagnostics":
            command.append("--diagnostics-only")
        logger.info("Running blend utility: %s", " ".join(command))
        subprocess.run(command, cwd=BLEND_UTILS, check=True, env=env)
        ran_blends.append(blend)
    return ran_blends


def _path_has_files(path: Path) -> bool:
    return path.exists() and any(child.is_file() for child in path.rglob("*"))


def _write_config_markers(
    common_bucket: str,
    date: str,
    region: str,
    run_mode: str,
    blends: list[BlendConfig],
) -> None:
    if run_mode in {"all", "blend"}:
        for blend in blends:
            if not _path_has_files(blend.output_dir(date)):
                raise click.ClickException(
                    f"Blend {region}/{blend.name} completed but no output files were found at "
                    f"{blend.output_dir(date)}"
                )
            write_gcs_text(
                common_bucket,
                f"intermediate/blend_{region}_{blend.name}_{date}_done",
                "done",
            )
    if run_mode in {"all", "diagnostics"}:
        for blend in blends:
            if not _path_has_files(blend.diagnostic_output_dir(date)):
                raise click.ClickException(
                    f"Diagnostics {region}/{blend.name} completed but no output files were found at "
                    f"{blend.diagnostic_output_dir(date)}"
                )
            write_gcs_text(
                common_bucket,
                f"intermediate/model_diagnostics_{region}_{blend.name}_{date}_done",
                "done",
            )


def _upload_outputs(
    date: str,
    region: str,
    region_bucket: str,
    common_bucket: str,
    run_mode: str,
    blends: list[BlendConfig],
) -> None:
    # india2025 writes to blend/output/india2025/ — upload everything under blend/output
    if run_mode in {"all", "blend"}:
        upload_directory(region_bucket, LOCAL_BLEND_OUT_BASE, f"output/blend/{date}")
        logger.info("Blend outputs uploaded to gs://%s/output/blend/%s/", region_bucket, date)
    if run_mode in {"all", "diagnostics"}:
        upload_directory(
            region_bucket,
            LOCAL_DIAGNOSTICS_OUT_BASE / region,
            f"output/model_diagnostics/{date}/{region}",
        )
        logger.info(
            "Model diagnostics uploaded to gs://%s/output/model_diagnostics/%s/%s/",
            region_bucket,
            date,
            region,
        )

    _write_config_markers(common_bucket, date, region, run_mode, blends)


if __name__ == "__main__":
    main()
