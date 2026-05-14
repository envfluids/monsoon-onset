"""
Monsoon GenCast - GCS Shim Wrapper

Downloads GenCast weights, normalization stats, ECMWF GRIB inputs, and SST ICs
from GCS into the paths expected by the original science scripts, runs GenCast
inference, then uploads outputs back to GCS.

Environment Variables:
    DATE                 : Forecast date YYYYMMDDTHH
    FORECAST_REGION      : e.g. 'india'
    GCS_BUCKET           : Main data bucket
    GRAPHCAST_BUCKET     : Bucket containing DeepMind GenCast assets
                           (default: dm_graphcast)
"""

import datetime as dt
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


def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob = _client().bucket(bucket_name).blob(gcs_path)
    blob.download_to_filename(str(local_path))
    logger.info("Downloaded gs://%s/%s -> %s", bucket_name, gcs_path, local_path)


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    client = _client()
    bucket = client.bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info("Uploaded %s -> gs://%s/%s", local_file, bucket_name, gcs_path)


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info("Wrote to gs://%s/%s", bucket_name, gcs_path)


@click.command()
@click.option("--date", envvar="DATE", required=True)
@click.option("--region", envvar="FORECAST_REGION", default="india")
@click.option("--bucket", envvar="GCS_BUCKET", required=True)
@click.option("--graphcast-bucket", envvar="GRAPHCAST_BUCKET", default=GRAPHCAST_BUCKET)
def main(date, region, bucket, graphcast_bucket):
    _setup_directories()
    _download_static_assets(graphcast_bucket)
    _download_inputs(date, region, bucket)
    _run_science_scripts(date)
    _upload_outputs(date, region, bucket)


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


def _download_inputs(date_f: str, region: str, bucket: str) -> None:
    _download_grib_inputs(date_f, region, bucket)
    download_gcs_file(
        bucket,
        f"{region}/raw/gencast/sst/{date_f}/sst_{date_f}.nc",
        GENCAST_UTILS.parent / "raw" / "sst_ic" / f"sst_{date_f}.nc",
    )


def _download_grib_inputs(date_f: str, region: str, bucket: str) -> None:
    for filename in _expected_ecmwf_grib_names(date_f):
        download_gcs_file(
            bucket,
            f"{region}/raw/ecmwf/{date_f}/grib/{filename}",
            AIFS_GRIB_DIR / filename,
        )


def _expected_ecmwf_grib_names(date_f: str) -> list[str]:
    date = dt.datetime.strptime(date_f, "%Y%m%dT%H")
    dates = [date - dt.timedelta(hours=12), date]
    return [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]


def _run_science_scripts(date: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(GENCAST_UTILS)}

    logger.info("Running GenCast run_gencast.py for %s", date)
    _log_gpu_runtime(env)
    subprocess.run(
        [sys.executable, "run_gencast.py", "--date", date],
        cwd=GENCAST_UTILS,
        check=True,
        env=env,
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
            cwd=GENCAST_UTILS,
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


def _upload_outputs(date: str, region: str, bucket: str) -> None:
    output_dir = GENCAST_UTILS.parent / "raw" / "output"
    gcs_prefix = f"{region}/output/gencast/{date}"
    upload_directory(bucket, output_dir, gcs_prefix)
    logger.info("GenCast outputs uploaded to gs://%s/%s/", bucket, gcs_prefix)
    write_gcs_text(bucket, f"{region}/intermediate/gencast_{date}_done", "done")


if __name__ == "__main__":
    main()
