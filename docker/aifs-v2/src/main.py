"""
Monsoon deterministic AIFS v2 GCS wrapper.

Runs the AIFS_single_v2 science entrypoint and per-region post-processing.
Cloud paths use the exact versioned model name.
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

REPO_ROOT = Path("/app")
AIFS_UTILS = REPO_ROOT / "AIFS" / "utils"
IC_ECMWF_DIR = REPO_ROOT / "IC" / "output" / "ecmwf"
MODEL_CONFIG_PATH = REPO_ROOT / "config" / "models.json"

MODEL_NAME = "AIFS_single_v2"
RUN_SCRIPT = "run_model.py"
OUTPUT_SUFFIX = ".nc"
SPARSE_DOWNLOAD_NAME = "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"
SPARSE_RUN_NAME = "7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz"
GCS_UPLOAD_WORKERS = int(os.environ.get("AIFS_GCS_UPLOAD_WORKERS", "16"))


def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info("Downloaded gs://%s/%s -> %s", bucket_name, gcs_path, local_path)


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info("Wrote gs://%s/%s", bucket_name, gcs_path)


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info("Uploaded %s -> gs://%s/%s", local_path, bucket_name, gcs_path)


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    local_files = [path for path in local_dir.rglob("*") if path.is_file()]
    logger.info(
        "Uploading %d files from %s to gs://%s/%s with %d workers",
        len(local_files),
        local_dir,
        bucket_name,
        gcs_prefix,
        GCS_UPLOAD_WORKERS,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=GCS_UPLOAD_WORKERS) as executor:
        futures = [
            executor.submit(_upload_directory_file, bucket_name, local_dir, path, gcs_prefix)
            for path in local_files
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def _upload_directory_file(
    bucket_name: str, local_dir: Path, local_file: Path, gcs_prefix: str
) -> None:
    relative = local_file.relative_to(local_dir)
    gcs_path = f"{gcs_prefix}/{relative}"
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_file))
    logger.info("Uploaded %s -> gs://%s/%s", local_file, bucket_name, gcs_path)


@click.command()
@click.option("--date", envvar="DATE", required=True)
@click.option(
    "--model",
    envvar="MODEL",
    default=MODEL_NAME,
    type=click.Choice([MODEL_NAME], case_sensitive=True),
)
@click.option("--regions", envvar="FORECAST_REGIONS", required=True)
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True)
@click.option(
    "--upload-full-field",
    envvar="UPLOAD_FULL_FIELD",
    type=lambda v: str(v).lower() == "true",
    default=True,
)
def main(date, model, regions, common_bucket, region_buckets, upload_full_field):
    regions = json.loads(regions)
    region_buckets = json.loads(region_buckets)

    logger.info(
        "AIFS v2 shim: model=%s date=%s regions=%s upload_full_field=%s",
        model,
        date,
        regions,
        upload_full_field,
    )

    _setup_directories(regions, model)
    _download_inputs(date, model, common_bucket)
    _run_inference(date, model)
    if upload_full_field:
        _upload_full_field(date, model, common_bucket)
    else:
        logger.info("UPLOAD_FULL_FIELD=false; skipping full-field upload")

    for region in regions:
        if region not in region_buckets:
            raise click.ClickException(f"No bucket configured for region {region!r}")
        _run_post_process(date, model, region)
        _upload_region_outputs(date, model, region, region_buckets[region])
        _write_completion_marker(date, model, region, common_bucket)


def _setup_directories(regions: list[str], model: str) -> None:
    base = [
        IC_ECMWF_DIR,
        AIFS_UTILS.parent / "output" / "raw" / model,
        AIFS_UTILS.parent / "weights",
        AIFS_UTILS.parent / "EKR" / "mir_16_linear",
        AIFS_UTILS.parent / "data",
        AIFS_UTILS.parent / "grids",
    ]
    for region in regions:
        for subdir in ("tp", "sji", "tcw"):
            base.append(AIFS_UTILS.parent / "output" / region / model / subdir)
    for path in base:
        path.mkdir(parents=True, exist_ok=True)


def _download_inputs(aifs_date: str, model: str, common_bucket: str) -> None:
    for filename in _expected_ecmwf_grib_names(aifs_date, model):
        download_gcs_file(
            common_bucket,
            f"ic/ecmwf/{aifs_date}/grib/{filename}",
            IC_ECMWF_DIR / filename,
        )

    weight_name = _model_config(model)["weights"]
    download_gcs_file(
        common_bucket,
        f"weights/aifs/{weight_name}",
        AIFS_UTILS.parent / "weights" / weight_name,
    )

    for sparse in (SPARSE_DOWNLOAD_NAME, SPARSE_RUN_NAME):
        download_gcs_file(
            common_bucket,
            f"weights/aifs/EKR/mir_16_linear/{sparse}",
            AIFS_UTILS.parent / "EKR" / "mir_16_linear" / sparse,
        )


def _model_config(model: str) -> dict:
    with open(MODEL_CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    return config[model]


def _expected_ecmwf_grib_names(date_str: str, model: str) -> list[str]:
    model_config = _model_config(model)
    date = datetime.strptime(date_str, "%Y%m%dT%H")
    deltas = sorted({0, int(model_config["ic_timedelta"])}, reverse=True)
    filenames = []
    for stream in model_config["ic_streams"]:
        for delta in deltas:
            target = date - timedelta(hours=delta)
            filenames.append(target.strftime(f"%Y%m%d%H0000-0h-{stream}-fc.grib2"))
    return filenames


def _run_inference(aifs_date: str, model: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(AIFS_UTILS)}
    command = [sys.executable, RUN_SCRIPT, "--date", aifs_date, "--model", model]
    logger.info("Running inference: %s", " ".join(command))
    subprocess.run(command, cwd=AIFS_UTILS, check=True, env=env)


def _run_post_process(aifs_date: str, model: str, region: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(AIFS_UTILS)}
    command = [
        sys.executable,
        "post_process.py",
        "--date",
        aifs_date,
        "--model",
        model,
        "--region",
        region,
    ]
    logger.info("Running post-process: %s", " ".join(command))
    subprocess.run(command, cwd=AIFS_UTILS, check=True, env=env)


def _upload_full_field(aifs_date: str, model: str, common_bucket: str) -> None:
    raw_path = AIFS_UTILS.parent / "output" / "raw" / model / f"init_{aifs_date}{OUTPUT_SUFFIX}"
    target = f"full_field/{model}/{aifs_date}/init_{aifs_date}{OUTPUT_SUFFIX}"
    if not raw_path.is_file():
        raise RuntimeError(f"Expected raw forecast output is missing: {raw_path}")
    upload_file(common_bucket, raw_path, target)


def _upload_region_outputs(aifs_date: str, model: str, region: str, region_bucket: str) -> None:
    local_dir = AIFS_UTILS.parent / "output" / region
    if not local_dir.exists():
        logger.warning("Region output directory is missing: %s", local_dir)
        return
    upload_directory(region_bucket, local_dir, f"output/{model}/{aifs_date}")


def _write_completion_marker(aifs_date: str, model: str, region: str, common_bucket: str) -> None:
    write_gcs_text(common_bucket, f"intermediate/{model}_{region}_{aifs_date}_done", "done")


if __name__ == "__main__":
    main()
