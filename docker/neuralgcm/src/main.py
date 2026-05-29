"""
Monsoon NeuralGCM — GCS Shim Wrapper

Runs the full NeuralGCM pipeline (preprocess → inference → post-process → merge)
using the original unmodified science scripts and uploads the per-region outputs
to the region buckets.

Multi-region behavior:
  1. Download IC + weights/forcings from the COMMON bucket (shared).
  2. Run inference + post-processing once.
  3. (Optional) Upload full-field raw forecast to the COMMON bucket.
  4. For each region in FORECAST_REGIONS, upload that region's post-processed
     outputs to its region bucket and write a per-(model, region) completion
     marker to the COMMON bucket.

Environment Variables:
    DATE              : Forecast date YYYYMMDDTHH
    FORECAST_REGIONS  : JSON list of regions, e.g. '["india"]'
    GCS_COMMON_BUCKET : Common bucket for ICs, weights, full-field, markers
    GCS_REGION_BUCKETS: JSON map {region: bucket} for post-processed outputs
    UPLOAD_FULL_FIELD : 'true' to upload raw forecast to common bucket (default false)
    NEURALGCM_ASYNC_SAVE_WORKERS: member Zarr save threads (default 1)
    NEURALGCM_ASYNC_SAVE_MAX_PENDING: bounded pending member saves (default 2)
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import click
from google.cloud import storage

LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - "
    "%(pathname)s:%(lineno)d - %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

NGCM_UTILS = Path("/app/NeuralGCM/utils")
IC_NCEP_DIR = Path("/app/IC/output/ncep")
COMMON_BUCKET_MOUNT = Path("/mnt/disks/common")

# Regions where the science layer currently produces post-processed outputs.
NGCM_SUPPORTED_REGIONS = {"india", "ethiopia"}


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    bucket = _client().bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info(f"Uploaded {local_file} → gs://{bucket_name}/{gcs_path}")


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}: {content!r}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",   envvar="DATE",  required=True)
@click.option("--regions", envvar="FORECAST_REGIONS", required=True,
              help="JSON list of regions to post-process for")
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True,
              help="JSON map {region: bucket}")
@click.option("--upload-full-field", envvar="UPLOAD_FULL_FIELD",
              type=lambda v: str(v).lower() == "true", default=False)
def main(date, regions, common_bucket, region_buckets, upload_full_field):
    regions = json.loads(regions)
    region_buckets = json.loads(region_buckets)

    logger.info(f"NeuralGCM shim: date={date} regions={regions} upload_full_field={upload_full_field}")

    raw_output_base = _raw_output_base(upload_full_field)

    _setup_directories(date, raw_output_base)
    _download_inputs(date, common_bucket)
    _run_science_scripts(date, raw_output_base)

    if upload_full_field:
        _upload_full_field(date, common_bucket, raw_output_base)
    else:
        logger.info("UPLOAD_FULL_FIELD=false — skipping NeuralGCM full-field upload")

    for region in regions:
        if region not in NGCM_SUPPORTED_REGIONS:
            logger.warning(
                "Region %r is not supported by the NeuralGCM science layer; skipping upload + marker",
                region,
            )
            continue
        if region not in region_buckets:
            raise click.ClickException(f"No bucket configured for region {region!r}")
        _upload_region_outputs(date, region, region_buckets[region])
        _write_completion_marker(date, region, common_bucket)


def _raw_output_base(upload_full_field: bool) -> Path:
    if upload_full_field:
        return COMMON_BUCKET_MOUNT / "full_field" / "neuralgcm"
    return NGCM_UTILS.parent / "output" / "raw"


def _setup_directories(date: str, raw_output_base: Path) -> None:
    for subdir in [
        "raw/ncep_ic/download",
        "raw/ncep_ic/processed",
        "output/india/sji",
        "output/india/tcw",
        "output/india/tp",
        "output/ethiopia/tp",
        "weights",
        "data/forcings",
        "data/model_ds",
    ]:
        (NGCM_UTILS.parent / subdir).mkdir(parents=True, exist_ok=True)
    (raw_output_base / date).mkdir(parents=True, exist_ok=True)
    IC_NCEP_DIR.mkdir(parents=True, exist_ok=True)


def _download_inputs(date: str, common_bucket: str) -> None:
    # 1. NCEP GDAS GRIB2 initial conditions
    download_gcs_file(
        common_bucket,
        f"ic/ncep/{date}/gdas_{date}.pgrb2",
        IC_NCEP_DIR / f"gdas_{date}.pgrb2",
    )

    # 2. NeuralGCM model checkpoint (loaded at run_model.py module level)
    model_name = "models_v1_precip_stochastic_precip_2_8_deg.pkl"
    download_gcs_file(
        common_bucket,
        f"weights/neuralgcm/{model_name}",
        NGCM_UTILS.parent / "weights" / model_name,
    )

    # 3. SST / Sea Ice climatology forcing (loaded at run_model.py module level)
    download_gcs_file(
        common_bucket,
        "weights/neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc",
        NGCM_UTILS.parent / "data" / "forcings" / "SST-SeaIce_clim_1979_2017_no_leap.nc",
    )


def _run_science_scripts(date: str, raw_output_base: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(NGCM_UTILS),
        "NEURALGCM_RAW_OUTPUT_DIR": str(raw_output_base),
    }

    logger.info(f"Running preprocess_ic.py for {date}")
    subprocess.run(
        [sys.executable, "preprocess_ic.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )

    logger.info(f"Running run_model.py for {date}")
    _log_gpu_runtime(env)
    subprocess.run(
        [sys.executable, "run_model.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )

    logger.info(f"Running post_process.py for {date}")
    subprocess.run(
        [sys.executable, "post_process.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )

    logger.info(f"Running post_process_merge.py for {date}")
    subprocess.run(
        [sys.executable, "post_process_merge.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )


def _log_gpu_runtime(env: dict[str, str]) -> None:
    checks = [
        ["nvidia-smi"],
        [
            sys.executable,
            "-c",
            (
                "import jax; "
                "print('jax', jax.__version__); "
                "print('backend', jax.default_backend()); "
                "print('devices', jax.devices())"
            ),
        ],
    ]
    for command in checks:
        result = subprocess.run(
            command,
            cwd=NGCM_UTILS,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        logger.info("%s exited %s", " ".join(command), result.returncode)
        if result.stdout:
            logger.info("%s stdout:\n%s", command[0], result.stdout)
        if result.stderr:
            logger.warning("%s stderr:\n%s", command[0], result.stderr)


def _upload_full_field(date: str, common_bucket: str, raw_output_base: Path) -> None:
    raw_dir = raw_output_base / date
    if _path_is_relative_to(raw_dir, COMMON_BUCKET_MOUNT):
        logger.info(
            "NeuralGCM full-field output was written through GCS FUSE to gs://%s/full_field/neuralgcm/%s/",
            common_bucket,
            date,
        )
        return
    upload_directory(common_bucket, raw_dir, f"full_field/neuralgcm/{date}")


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _upload_region_outputs(date: str, region: str, region_bucket: str) -> None:
    local_dir = NGCM_UTILS.parent / "output" / region
    if not local_dir.exists():
        logger.warning("Region output directory is missing: %s", local_dir)
        return
    upload_directory(region_bucket, local_dir, f"output/neuralgcm/{date}")


def _write_completion_marker(date: str, region: str, common_bucket: str) -> None:
    marker_path = f"intermediate/neuralgcm_{region}_{date}_done"
    write_gcs_text(common_bucket, marker_path, "done")


if __name__ == "__main__":
    main()
