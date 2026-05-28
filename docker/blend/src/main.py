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

BLEND_MODEL_TO_PIPELINE_MODEL = {
    "AIFS_SINGLE_V2": "AIFS_single_v2",
    "AIFS_ENS_V2": "AIFS_ENS_v2",
    "GENCAST": "gencast",
    "NCUM": "ncum",
    "NGCM": "neuralgcm",
    "NEURALGCM": "neuralgcm",
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
def main(date, region, run_mode, common_bucket, region_buckets):
    region_buckets = json.loads(region_buckets)
    if region not in region_buckets:
        raise click.ClickException(f"No bucket configured for region {region!r}")
    region_bucket = region_buckets[region]

    logger.info("Blend shim: region=%s date=%s run_mode=%s", region, date, run_mode)

    blends = _select_blends(region, run_mode)

    _setup_directories(blends, date, run_mode)
    _download_inputs(date, region, region_bucket, common_bucket, blends, run_mode)
    _run_blend(date, region, run_mode, blends)
    _upload_outputs(date, region, region_bucket, run_mode)


def _pipeline_models_for_blend(blend: BlendConfig) -> set[str]:
    return {
        BLEND_MODEL_TO_PIPELINE_MODEL.get(model.upper(), model)
        for model in blend.models()
    }


def _select_blends(region: str, run_mode: str) -> list[BlendConfig]:
    configured_models = set(REGIONS.get(region, {}).get("models", []))
    blends = [
        blend for blend in BLENDS
        if blend.region == region
        and (
            (run_mode in {"all", "blend"} and blend.implemented)
            or (run_mode in {"all", "diagnostics"} and blend.diagnostic_plots)
        )
        and (not configured_models or _pipeline_models_for_blend(blend).issubset(configured_models))
    ]
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


def _download_inputs(
    date: str,
    region: str,
    region_bucket: str,
    common_bucket: str,
    blends: list[BlendConfig],
    run_mode: str,
) -> None:
    for blend in blends:
        for input_ in _inputs_for_mode(blend, run_mode):
            download_gcs_file(
                region_bucket,
                _blend_input_bucket_path(region, input_, date),
                input_.path(date),
            )

    if run_mode == "diagnostics" or not any(blend.implemented for blend in blends):
        return

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


def _pipeline_model_for_blend_input(input_: ForecastInput) -> str:
    return BLEND_MODEL_TO_PIPELINE_MODEL.get(input_.model.upper(), input_.model)


def _blend_input_bucket_path(region: str, input_: ForecastInput, date: str) -> str:
    model = _pipeline_model_for_blend_input(input_)
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


def _run_blend(date: str, region: str, run_mode: str, blends: list[BlendConfig]) -> None:
    env = {**os.environ, "PYTHONPATH": str(BLEND_UTILS)}
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


def _upload_outputs(date: str, region: str, region_bucket: str, run_mode: str) -> None:
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


if __name__ == "__main__":
    main()
