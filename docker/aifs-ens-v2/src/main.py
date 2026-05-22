"""
Monsoon AIFS — GCS Shim Wrapper

Runs AIFS (deterministic) or AIFS-ENS inference and per-region post-processing
using the original unmodified science scripts.

Multi-region behavior:
  1. Download IC + weights from the COMMON bucket (shared across regions).
  2. Run inference once (the expensive step).
  3. Upload the full-field raw forecast to the COMMON bucket.
  4. For each region in FORECAST_REGIONS, run post_process.py --region {region}
     and upload the per-region products to that region's bucket.
  5. Write per-(model, region) completion markers to the COMMON bucket.

Environment Variables:
    DATE              : ECMWF 00z cycle date YYYYMMDDTHH (the AIFS init date)
    MODEL             : 'aifs' or 'aifs_ens'
    FORECAST_REGIONS  : JSON list of regions to post-process for, e.g. '["india","ethiopia"]'
    GCS_COMMON_BUCKET : Common bucket for ICs, weights, full-field, markers
    GCS_REGION_BUCKETS: JSON map {region: bucket} for post-processed outputs
    UPLOAD_FULL_FIELD : 'true' to upload raw forecast to common bucket (default true)
"""

import concurrent.futures
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

AIFS_UTILS = Path("/app/AIFS/utils")
SPARSE_DOWNLOAD_NAME = "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"
SPARSE_RUN_NAME      = "7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz"

MODEL_WEIGHT_PATHS = {
    "aifs":     ("weights/aifs/aifs-single-mse-1.1.ckpt", "aifs-single-mse-1.1.ckpt"),
    "aifs_ens": ("weights/aifs/aifs-ens-crps-1.0.ckpt",   "aifs-ens-crps-1.0.ckpt"),
}

RUN_SCRIPT = {
    "aifs":     "run_model.py",
    "aifs_ens": "run_model_ENS.py",
}

# Model name passed to post_process.py (matches its `--model` choice)
POST_PROCESS_MODEL = {
    "aifs":     "AIFS",
    "aifs_ens": "AIFS_ENS",
}

RAW_OUTPUT_SUBDIR = {
    "aifs":     "AIFS",
    "aifs_ens": "AIFS_ENS",
}

COMMON_BUCKET_MOUNT = Path("/mnt/disks/common")
AIFS_ENS_MIRROR_WORKERS = "16"
GCS_UPLOAD_WORKERS = int(os.environ.get("AIFS_GCS_UPLOAD_WORKERS", "16"))


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}: {content!r}")


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    local_files = [
        local_file for local_file in local_dir.rglob("*") if local_file.is_file()
    ]
    logger.info(
        "Uploading %d files from %s to gs://%s/%s with %d workers",
        len(local_files),
        local_dir,
        bucket_name,
        gcs_prefix,
        GCS_UPLOAD_WORKERS,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=GCS_UPLOAD_WORKERS
    ) as executor:
        futures = [
            executor.submit(
                _upload_directory_file, bucket_name, local_dir, local_file, gcs_prefix
            )
            for local_file in local_files
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def _upload_directory_file(
    bucket_name: str, local_dir: Path, local_file: Path, gcs_prefix: str
) -> None:
    relative = local_file.relative_to(local_dir)
    gcs_path = f"{gcs_prefix}/{relative}"
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_file))
    logger.info(f"Uploaded {local_file} → gs://{bucket_name}/{gcs_path}")


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info(f"Uploaded {local_path} → gs://{bucket_name}/{gcs_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",   envvar="DATE",  required=True,
              help="ECMWF 00z cycle date YYYYMMDDTHH (the AIFS init date)")
@click.option("--model",  envvar="MODEL", default="aifs",
              type=click.Choice(["aifs", "aifs_ens"], case_sensitive=False))
@click.option("--regions", envvar="FORECAST_REGIONS", required=True,
              help="JSON list of regions to post-process for")
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True,
              help="JSON map {region: bucket}")
@click.option("--upload-full-field", envvar="UPLOAD_FULL_FIELD",
              type=lambda v: str(v).lower() == "true", default=True)
def main(date, model, regions, common_bucket, region_buckets, upload_full_field):
    model = model.lower()
    regions = json.loads(regions)
    region_buckets = json.loads(region_buckets)

    logger.info(f"AIFS shim: model={model} date={date} regions={regions} upload_full_field={upload_full_field}")

    _setup_directories(regions)
    _download_inputs(date, model, common_bucket)
    zarr_mirror_target = _aifs_ens_zarr_mirror_target(date, model, upload_full_field)
    _run_inference(date, model, zarr_mirror_target)
    if upload_full_field and zarr_mirror_target is None and model != "aifs_ens":
        _upload_full_field(date, model, common_bucket)
    elif zarr_mirror_target is not None:
        logger.info("AIFS-ENS full-field Zarr was mirrored through GCS FUSE")
    else:
        logger.info("UPLOAD_FULL_FIELD=false — skipping AIFS full-field upload")
    for region in regions:
        if region not in region_buckets:
            raise click.ClickException(f"No bucket configured for region {region!r}")
        _run_post_process(date, model, region)
        _upload_region_outputs(date, model, region, region_buckets[region])
        _write_completion_marker(date, model, region, common_bucket)


def _setup_directories(regions: list[str]) -> None:
    base = [
        "raw/ifs_ic/grib",
        "raw/output/AIFS",
        "raw/output/AIFS_ENS",
        "weights",
        "EKR/mir_16_linear",
        "data",
        "grids",
    ]
    region_dirs = []
    for region in regions:
        if region == "india":
            region_dirs += ["output/india/sji", "output/india/tcw", "output/india/tp"]
        elif region == "ethiopia":
            region_dirs += ["output/ethiopia/AIFS/tp", "output/ethiopia/AIFS_ENS/tp"]
    for subdir in base + region_dirs:
        (AIFS_UTILS.parent / subdir).mkdir(parents=True, exist_ok=True)


def _download_inputs(aifs_date: str, model: str, common_bucket: str) -> None:
    # 1. ECMWF GRIB initial conditions
    for filename in _expected_ecmwf_grib_names(aifs_date):
        download_gcs_file(
            common_bucket,
            f"ic/ecmwf/{aifs_date}/grib/{filename}",
            AIFS_UTILS.parent / "raw" / "ifs_ic" / "grib" / filename,
        )

    # 2. Model weights for the selected variant
    gcs_path, filename = MODEL_WEIGHT_PATHS[model]
    download_gcs_file(
        common_bucket,
        gcs_path,
        AIFS_UTILS.parent / "weights" / filename,
    )

    # 3. Sparse transform matrices used by preprocess_ic.py and run_model*.py
    for sparse in (SPARSE_DOWNLOAD_NAME, SPARSE_RUN_NAME):
        download_gcs_file(
            common_bucket,
            f"weights/aifs/EKR/mir_16_linear/{sparse}",
            AIFS_UTILS.parent / "EKR" / "mir_16_linear" / sparse,
        )


def _expected_ecmwf_grib_names(date_str: str) -> list[str]:
    date = datetime.strptime(date_str, "%Y%m%dT%H")
    dates = [date - timedelta(hours=12), date - timedelta(hours=6), date]
    return [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]


def _aifs_ens_zarr_mirror_target(
    aifs_date: str, model: str, upload_full_field: bool
) -> Path | None:
    if model != "aifs_ens" or not upload_full_field:
        return None

    if not COMMON_BUCKET_MOUNT.is_dir():
        raise RuntimeError(
            "AIFS-ENS full-field upload requires the GCS FUSE mount at "
            f"{COMMON_BUCKET_MOUNT}, but it is unavailable."
        )

    target = (
        COMMON_BUCKET_MOUNT
        / "full_field"
        / model
        / aifs_date
        / f"init_{aifs_date}.zarr"
    )
    logger.info("AIFS-ENS full-field Zarr mirror target: %s", target)
    return target


def _run_inference(
    aifs_date: str, model: str, zarr_mirror_target: Path | None = None
) -> None:
    env = {**os.environ, "PYTHONPATH": str(AIFS_UTILS)}
    if zarr_mirror_target is not None:
        env["AIFS_ENS_ZARR_MIRROR_TARGET"] = str(zarr_mirror_target)
        env["AIFS_ENS_ZARR_MIRROR_WORKERS"] = AIFS_ENS_MIRROR_WORKERS
    script = RUN_SCRIPT[model]
    logger.info(f"Running {model} {script} for {aifs_date}")
    subprocess.run(
        [sys.executable, script, "--date", aifs_date],
        cwd=AIFS_UTILS, check=True, env=env,
    )


def _run_post_process(aifs_date: str, model: str, region: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(AIFS_UTILS)}
    logger.info(f"Running post_process.py for {model} {region} {aifs_date}")
    subprocess.run(
        [
            sys.executable, "post_process.py",
            "--date", aifs_date,
            "--model", POST_PROCESS_MODEL[model],
            "--region", region,
        ],
        cwd=AIFS_UTILS, check=True, env=env,
    )


def _upload_full_field(aifs_date: str, model: str, common_bucket: str) -> None:
    raw_dir = AIFS_UTILS.parent / "raw" / "output" / RAW_OUTPUT_SUBDIR[model]
    raw_name = f"init_{aifs_date}.nc" if model == "aifs" else f"init_{aifs_date}.zarr"
    raw_path = raw_dir / raw_name
    target_prefix = f"full_field/{model}/{aifs_date}/{raw_name}"

    if raw_path.is_dir():
        upload_directory(common_bucket, raw_path, target_prefix)
    elif raw_path.is_file():
        upload_file(common_bucket, raw_path, target_prefix)
    else:
        raise RuntimeError(f"Expected raw forecast output is missing: {raw_path}")


def _upload_region_outputs(aifs_date: str, model: str, region: str, region_bucket: str) -> None:
    local_dir = AIFS_UTILS.parent / "output" / region
    if not local_dir.exists():
        logger.warning("Region output directory is missing: %s", local_dir)
        return
    upload_directory(region_bucket, local_dir, f"output/{model}/{aifs_date}")


def _write_completion_marker(aifs_date: str, model: str, region: str, common_bucket: str) -> None:
    marker_path = f"intermediate/{model}_{region}_{aifs_date}_done"
    write_gcs_text(common_bucket, marker_path, "done")


if __name__ == "__main__":
    main()
