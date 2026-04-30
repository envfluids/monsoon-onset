"""
Monsoon NeuralGCM — GCS Shim Wrapper

Downloads NCEP initial conditions and model weights from GCS, runs the full
NeuralGCM pipeline (preprocess → inference → post-process → merge) using
the original unmodified science scripts, then uploads outputs to GCS.

The science scripts use relative paths from their utils/ directory, so we
set cwd=/app/NeuralGCM/utils/ before each subprocess call. Model weights and
the ERA5 reference file are loaded at module level in run_model.py, so all
required files must be in place before the subprocess is invoked.

Environment Variables:
    DATE               : Forecast date YYYYMMDDTHH
    FORECAST_REGION    : e.g. 'india'
    GCS_BUCKET         : Main data bucket
    GCS_WEIGHTS_BUCKET : Weights/static-files bucket
"""

import os
import sys
import logging
import subprocess
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


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _client():
    return storage.Client()


def download_gcs_file(bucket_name: str, gcs_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob = _client().bucket(bucket_name).blob(gcs_path)
    blob.download_to_filename(str(local_path))
    logger.info(f"Downloaded gs://{bucket_name}/{gcs_path} → {local_path}")


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    client = _client()
    bucket = client.bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info(f"Uploaded {local_file} → gs://{bucket_name}/{gcs_path}")


def write_gcs_text(bucket_name: str, gcs_path: str, content: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_string(content)
    logger.info(f"Wrote to gs://{bucket_name}/{gcs_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",           envvar="DATE",              required=True)
@click.option("--region",         envvar="FORECAST_REGION",   default="india")
@click.option("--bucket",         envvar="GCS_BUCKET",        required=True)
@click.option("--weights-bucket", envvar="GCS_WEIGHTS_BUCKET", required=True)
def main(date, region, bucket, weights_bucket):
    _setup_directories(date)
    _download_inputs(date, region, bucket, weights_bucket)
    _run_science_scripts(date)
    _upload_outputs(date, region, bucket)


def _setup_directories(date: str) -> None:
    for subdir in [
        "raw/ncep_ic/download",
        "raw/ncep_ic/processed",
        f"raw/output/{date}",
        "output/sji",
        "output/tcw",
        "output/tp",
        "weights",
        "data/forcings",
        "data/model_ds",
    ]:
        (NGCM_UTILS.parent / subdir).mkdir(parents=True, exist_ok=True)


def _download_inputs(date: str, region: str, bucket: str, weights_bucket: str) -> None:
    # 1. NCEP GDAS GRIB2 initial conditions
    download_gcs_file(
        bucket,
        f"{region}/raw/ncep/{date}/gdas_{date}.pgrb2",
        NGCM_UTILS.parent / "raw" / "ncep_ic" / "download" / f"gdas_{date}.pgrb2",
    )

    # 2. NeuralGCM model checkpoint (loaded at run_model.py module level)
    model_name = "models_v1_precip_stochastic_precip_2_8_deg.pkl"
    download_gcs_file(
        weights_bucket,
        f"neuralgcm/{model_name}",
        NGCM_UTILS.parent / "weights" / model_name,
    )

    # 3. SST / Sea Ice climatology forcing (loaded at run_model.py module level)
    download_gcs_file(
        weights_bucket,
        "neuralgcm/forcings/SST-SeaIce_clim_1979_2017_no_leap.nc",
        NGCM_UTILS.parent / "data" / "forcings" / "SST-SeaIce_clim_1979_2017_no_leap.nc",
    )


def _run_science_scripts(date: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(NGCM_UTILS)}

    # Step 1: NCL interpolation of GRIB2 → NetCDF
    logger.info(f"Running preprocess.py for {date}")
    subprocess.run(
        [sys.executable, "preprocess.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )

    # Step 2: Ensemble inference (all 30 members; --mpi omitted → runs all on available devices)
    # Note: run_model.py loads weights and ERA5 at module level, so all files must be present.
    logger.info(f"Running run_model.py for {date}")
    _log_gpu_runtime(env)
    subprocess.run(
        [sys.executable, "run_model.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )

    # Step 3: Post-process individual ensemble members
    logger.info(f"Running post_process.py for {date}")
    subprocess.run(
        [sys.executable, "post_process.py", "--date", date],
        cwd=NGCM_UTILS, check=True, env=env,
    )

    # Step 4: Merge ensemble members and compute SJI
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


def _upload_outputs(date: str, region: str, bucket: str) -> None:
    output_dir = NGCM_UTILS.parent / "output"
    gcs_prefix = f"{region}/output/neuralgcm/{date}"
    upload_directory(bucket, output_dir, gcs_prefix)
    logger.info(f"NeuralGCM outputs uploaded to gs://{bucket}/{gcs_prefix}/")

    # Write completion marker so the workflow polling loop knows we're done
    write_gcs_text(bucket, f"{region}/intermediate/neuralgcm_{date}_done", "done")


if __name__ == "__main__":
    main()
