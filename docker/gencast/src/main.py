"""
Monsoon GenCast — GCS Shim Wrapper

Multi-region behavior (same pattern as AIFS / NeuralGCM):
  1. Download IC (ECMWF GRIB + GenCast SST) + weights/stats from the COMMON
     bucket (shared across regions).
  2. Run inference once (the expensive step).
  3. (Optional) Upload full-field raw forecast to the COMMON bucket.
  4. For each region in FORECAST_REGIONS, run region post-processing, upload
     the per-region GenCast output, and write a per-(model, region) marker.

Environment Variables:
    DATE              : ECMWF 00z cycle date YYYYMMDDTHH (the GenCast init date)
    FORECAST_REGIONS  : JSON list of regions, e.g. '["ethiopia"]'
    GCS_COMMON_BUCKET : Common bucket for ICs, weights, full-field, markers
    GCS_REGION_BUCKETS: JSON map {region: bucket} for per-region outputs
    UPLOAD_FULL_FIELD : 'true' to upload raw forecast to common bucket (default true)
    GRAPHCAST_BUCKET  : DeepMind GenCast assets bucket (default: dm_graphcast)
"""

import datetime as dt
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

GENCAST_UTILS = Path("/app/gencast/utils")
AIFS_GRIB_DIR = Path("/app/AIFS/raw/ifs_ic/grib")

GRAPHCAST_BUCKET = "dm_graphcast"
MODEL_NAME = "GenCast 0p25deg Operational <2022.npz"
STATS_FILES = [
    "diffs_stddev_by_level.nc",
    "mean_by_level.nc",
    "stddev_by_level.nc",
    "min_by_level.nc",
]

MODEL = "gencast"


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client().bucket(bucket_name).blob(gcs_path).download_to_filename(str(local_path))
    logger.info("Downloaded gs://%s/%s -> %s", bucket_name, gcs_path, local_path)


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    bucket = _client().bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info("Uploaded %s -> gs://%s/%s", local_file, bucket_name, gcs_path)


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info("Uploaded %s -> gs://%s/%s", local_path, bucket_name, gcs_path)


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info("Wrote to gs://%s/%s", bucket_name, gcs_path)


def write_dispatch_status(
    date: str,
    state: str,
    message: str,
    exit_code: int | None = None,
) -> None:
    bucket_name = os.getenv("TPU_DISPATCH_STATUS_BUCKET")
    status_path = os.getenv("TPU_DISPATCH_STATUS_PATH")
    if not bucket_name or not status_path:
        return
    payload = {
        "run_id": os.getenv("TPU_DISPATCH_RUN_ID", ""),
        "attempt": int(os.getenv("TPU_DISPATCH_ATTEMPT", "0")),
        "workload": os.getenv("TPU_DISPATCH_WORKLOAD", MODEL),
        "date": date,
        "state": state,
        "queued_resource_id": os.getenv("TPU_DISPATCH_QUEUED_RESOURCE_ID", ""),
        "node_id": os.getenv("TPU_DISPATCH_NODE_ID", ""),
        "zone": os.getenv("TPU_DISPATCH_ZONE", ""),
        "started_at": "",
        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        "message": message,
        "exit_code": exit_code,
    }
    _client().bucket(bucket_name).blob(status_path).upload_from_string(
        json.dumps(payload, sort_keys=True),
        content_type="application/json",
    )
    logger.info("Wrote TPU dispatch status %s to gs://%s/%s", state, bucket_name, status_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date", envvar="DATE", required=True,
              help="ECMWF 00z cycle date YYYYMMDDTHH (the GenCast init date)")
@click.option("--regions", envvar="FORECAST_REGIONS", required=True,
              help="JSON list of regions to publish outputs for")
@click.option("--common-bucket", envvar="GCS_COMMON_BUCKET", required=True)
@click.option("--region-buckets", envvar="GCS_REGION_BUCKETS", required=True,
              help="JSON map {region: bucket}")
@click.option("--upload-full-field", envvar="UPLOAD_FULL_FIELD",
              type=lambda v: str(v).lower() == "true", default=True)
@click.option("--graphcast-bucket", envvar="GRAPHCAST_BUCKET", default=GRAPHCAST_BUCKET)
def main(date, regions, common_bucket, region_buckets, upload_full_field, graphcast_bucket):
    try:
        _main(date, regions, common_bucket, region_buckets, upload_full_field, graphcast_bucket)
    except Exception as exc:
        write_dispatch_status(date, "FAILED", f"GenCast shim failed: {exc}", 1)
        raise


def _main(date, regions, common_bucket, region_buckets, upload_full_field, graphcast_bucket):
    regions = json.loads(regions)
    region_buckets = json.loads(region_buckets)

    logger.info(
        f"GenCast shim: date={date} regions={regions} upload_full_field={upload_full_field}"
    )

    _setup_directories()
    _download_static_assets(graphcast_bucket)
    _download_inputs(date, common_bucket)
    run_metadata = _run_inference(date)

    process_index = int(run_metadata.get("process_index", 0))
    process_count = int(run_metadata.get("process_count", 1))
    if process_index != 0:
        logger.info(
            "GenCast JAX process %s/%s completed inference; process 0 publishes GCS outputs.",
            process_index,
            process_count,
        )
        return

    if upload_full_field:
        _upload_full_field(date, common_bucket)
    else:
        logger.info("UPLOAD_FULL_FIELD=false — skipping GenCast full-field upload")

    for region in regions:
        if region not in region_buckets:
            raise click.ClickException(f"No bucket configured for region {region!r}")
        _upload_region_outputs(date, region, region_buckets[region])
        _write_completion_marker(date, region, common_bucket)
    write_dispatch_status(date, "SUCCEEDED", "GenCast outputs uploaded", 0)


def _setup_directories() -> None:
    for path in [
        GENCAST_UTILS.parent / "weights",
        GENCAST_UTILS.parent / "data",
        GENCAST_UTILS.parent / "raw" / "sst_ic",
        GENCAST_UTILS.parent / "raw" / "output",
        AIFS_GRIB_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def _download_static_assets(graphcast_bucket: str) -> None:
    download_gcs_file(
        graphcast_bucket,
        f"gencast/params/{MODEL_NAME}",
        GENCAST_UTILS.parent / "weights" / MODEL_NAME,
    )

    for filename in STATS_FILES:
        download_gcs_file(
            graphcast_bucket,
            f"gencast/stats/{filename}",
            GENCAST_UTILS.parent / "data" / filename,
        )


def _download_inputs(date: str, common_bucket: str) -> None:
    _download_grib_inputs(date, common_bucket)
    download_gcs_file(
        common_bucket,
        f"ic/gencast_sst/{date}/sst_{date}.nc",
        GENCAST_UTILS.parent / "raw" / "sst_ic" / f"sst_{date}.nc",
    )


def _download_grib_inputs(date: str, common_bucket: str) -> None:
    for filename in _expected_ecmwf_grib_names(date):
        download_gcs_file(
            common_bucket,
            f"ic/ecmwf/{date}/grib/{filename}",
            AIFS_GRIB_DIR / filename,
        )


def _expected_ecmwf_grib_names(date: str) -> list[str]:
    base = dt.datetime.strptime(date, "%Y%m%dT%H")
    dates = [base - dt.timedelta(hours=12), base]
    return [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]


def _run_inference(date: str) -> dict[str, object]:
    env = {**os.environ, "PYTHONPATH": str(GENCAST_UTILS)}
    logger.info("Running GenCast run_gencast.py for %s", date)
    _log_jax_runtime(env)
    subprocess.run(
        [sys.executable, "run_gencast.py", "--date", date],
        cwd=GENCAST_UTILS, check=True, env=env,
    )
    return _read_run_metadata(date)


def _run_post_process(date: str, region: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(GENCAST_UTILS)}
    logger.info("Running GenCast post_process.py for %s region=%s", date, region)
    subprocess.run(
        [sys.executable, "post_process.py", "--date", date, "--region", region],
        cwd=GENCAST_UTILS,
        check=True,
        env=env,
    )


def _log_jax_runtime(env: dict[str, str]) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import jax; print('jax', jax.__version__)",
        ],
        cwd=GENCAST_UTILS,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    logger.info("JAX version probe exited %s", result.returncode)
    if result.stdout:
        logger.info("JAX version probe stdout:\n%s", result.stdout)
    if result.stderr:
        logger.warning("JAX version probe stderr:\n%s", result.stderr)


def _read_run_metadata(date: str) -> dict[str, object]:
    metadata_path = GENCAST_UTILS.parent / "raw" / "output" / f"run_metadata_{date}.json"
    if not metadata_path.exists():
        raise RuntimeError(f"Expected GenCast run metadata is missing: {metadata_path}")
    with metadata_path.open() as f:
        metadata = json.load(f)
    logger.info("GenCast run metadata: %s", metadata)
    return metadata


def _upload_full_field(date: str, common_bucket: str) -> None:
    raw_path = GENCAST_UTILS.parent / "raw" / "output" / f"init_{date}.zarr"
    if not raw_path.exists():
        raise RuntimeError(f"Expected raw GenCast forecast is missing: {raw_path}")
    upload_directory(common_bucket, raw_path, f"full_field/gencast/{date}/init_{date}.zarr")


def _upload_region_outputs(date: str, region: str, region_bucket: str) -> None:
    """Publish the per-region GenCast output.

    Run the region post-processing script against the raw Zarr forecast, then
    upload the region-specific precipitation product.
    """
    _run_post_process(date, region)
    tp_path = GENCAST_UTILS.parent / "output" / region / "tp" / f"tp_0p25_{date}.nc"
    if not tp_path.exists():
        raise RuntimeError(f"Expected raw GenCast forecast is missing: {tp_path}")
    upload_file(
        region_bucket,
        tp_path,
        f"output/gencast/{date}/tp_0p25_{date}.nc",
    )


def _write_completion_marker(date: str, region: str, common_bucket: str) -> None:
    write_gcs_text(common_bucket, f"intermediate/{MODEL}_{region}_{date}_done", "done")


if __name__ == "__main__":
    main()
