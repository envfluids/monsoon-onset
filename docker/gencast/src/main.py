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
IC_ECMWF_DIR = Path("/app/IC/output/ecmwf")
COMMON_BUCKET_MOUNT = Path("/mnt/disks/common")

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
    run_metadata = _run_inference(date, common_bucket, upload_full_field)

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
        mirror_target = os.getenv("GENCAST_ZARR_MIRROR_TARGET") or str(
            _gcs_fuse_mirror_target(date)
        )
        logger.info(
            "GenCast full-field Zarr was written during inference via "
            "Cloud Storage FUSE at %s (gs://%s/full_field/gencast/%s/init_%s.zarr/)",
            mirror_target,
            common_bucket,
            date,
            date,
        )
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
        IC_ECMWF_DIR,
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
        IC_ECMWF_DIR / f"sst_{date}.nc",
    )


def _download_grib_inputs(date: str, common_bucket: str) -> None:
    for filename in _expected_ecmwf_grib_names(date):
        download_gcs_file(
            common_bucket,
            f"ic/ecmwf/{date}/grib/{filename}",
            IC_ECMWF_DIR / filename,
        )


def _expected_ecmwf_grib_names(date: str) -> list[str]:
    base = dt.datetime.strptime(date, "%Y%m%dT%H")
    dates = [base - dt.timedelta(hours=12), base]
    return [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]


def _run_inference(
    date: str,
    common_bucket: str,
    upload_full_field: bool,
) -> dict[str, object]:
    env = {**os.environ, "PYTHONPATH": str(GENCAST_UTILS)}
    if _jax_compilation_cache_enabled(env):
        env.setdefault("GENCAST_GCSFUSE_BUCKET", common_bucket)
        env.setdefault("GENCAST_GCSFUSE_MOUNT", str(COMMON_BUCKET_MOUNT))
        env.setdefault(
            "JAX_COMPILATION_CACHE_DIR",
            str(_jax_compilation_cache_dir()),
        )
        env.setdefault("JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS", "1")
        logger.info(
            "Using shared JAX compilation cache at %s via Cloud Storage FUSE bucket gs://%s.",
            env["JAX_COMPILATION_CACHE_DIR"],
            env["GENCAST_GCSFUSE_BUCKET"],
        )
    if upload_full_field:
        env.setdefault("GENCAST_GCSFUSE_BUCKET", common_bucket)
        env.setdefault("GENCAST_GCSFUSE_MOUNT", str(COMMON_BUCKET_MOUNT))
        env.setdefault("GENCAST_OUTPUT_DIR", str(_gcs_fuse_output_dir(date)))
        os.environ.setdefault("GENCAST_GCSFUSE_BUCKET", env["GENCAST_GCSFUSE_BUCKET"])
        os.environ.setdefault("GENCAST_GCSFUSE_MOUNT", env["GENCAST_GCSFUSE_MOUNT"])
        os.environ.setdefault("GENCAST_OUTPUT_DIR", env["GENCAST_OUTPUT_DIR"])
        if env.get("GENCAST_ZARR_MIRROR_TARGET"):
            logger.info(
                "Using preconfigured filesystem/Cloud Storage FUSE GenCast Zarr mirror target: %s",
                env["GENCAST_ZARR_MIRROR_TARGET"],
            )
        elif _path_is_relative_to(env["GENCAST_OUTPUT_DIR"], env["GENCAST_GCSFUSE_MOUNT"]):
            logger.info(
                "Writing GenCast full-field Zarr directly to container-mounted "
                "Cloud Storage FUSE output directory: %s (bucket=gs://%s)",
                env["GENCAST_OUTPUT_DIR"],
                common_bucket,
            )
        else:
            env["GENCAST_ZARR_MIRROR_TARGET"] = str(_gcs_fuse_mirror_target(date))
            logger.info(
                "Using container-mounted Cloud Storage FUSE GenCast Zarr mirror target: %s "
                "(bucket=gs://%s, mount=%s)",
                env["GENCAST_ZARR_MIRROR_TARGET"],
                common_bucket,
                env["GENCAST_GCSFUSE_MOUNT"],
            )
    logger.info("Running GenCast run_gencast.py for %s", date)
    _log_jax_runtime(env)
    subprocess.run(
        [sys.executable, "run_gencast.py", "--date", date],
        cwd=GENCAST_UTILS, check=True, env=env,
    )
    return _read_run_metadata(date)


def _gcs_fuse_mirror_target(date: str) -> Path:
    mount = Path(os.getenv("GENCAST_GCSFUSE_MOUNT", str(COMMON_BUCKET_MOUNT)))
    return mount / "full_field" / "gencast" / date / f"init_{date}.zarr"


def _gcs_fuse_output_dir(date: str) -> Path:
    mount = Path(os.getenv("GENCAST_GCSFUSE_MOUNT", str(COMMON_BUCKET_MOUNT)))
    return mount / "full_field" / "gencast" / date


def _path_is_relative_to(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _jax_compilation_cache_dir() -> Path:
    mount = Path(os.getenv("GENCAST_GCSFUSE_MOUNT", str(COMMON_BUCKET_MOUNT)))
    return mount / "jax-cache" / "gencast" / "v5p-64"


def _jax_compilation_cache_enabled(env: dict[str, str]) -> bool:
    value = env.get("GENCAST_ENABLE_JAX_COMPILATION_CACHE")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return env.get("GENCAST_JAX_DISTRIBUTED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


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
