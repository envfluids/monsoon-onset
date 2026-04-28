"""
Monsoon AIFS — GCS Shim Wrapper

Downloads ECMWF initial conditions and model weights from GCS, runs AIFS
inference and post-processing using the original unmodified science scripts,
then uploads outputs back to GCS.

The science scripts (run_model.py, post_process.py) use relative paths from
their utils/ directory, so we set cwd=/app/AIFS/utils/ before invoking them.

Environment Variables:
    DATE               : NeuralGCM date YYYYMMDDTHH. The AIFS IC date is
                         auto-computed as DATE - 12h (matching blend expectations).
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

AIFS_UTILS = Path("/app/AIFS/utils")


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


def read_gcs_text(bucket_name: str, gcs_path: str) -> str:
    blob = _client().bucket(bucket_name).blob(gcs_path)
    return blob.download_as_text().strip()


def upload_directory(bucket_name: str, local_dir: Path, gcs_prefix: str) -> None:
    client = _client()
    bucket = client.bucket(bucket_name)
    for local_file in local_dir.rglob("*"):
        if local_file.is_file():
            relative = local_file.relative_to(local_dir)
            gcs_path = f"{gcs_prefix}/{relative}"
            bucket.blob(gcs_path).upload_from_filename(str(local_file))
            logger.info(f"Uploaded {local_file} → gs://{bucket_name}/{gcs_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",           envvar="DATE",              required=True)
@click.option("--region",         envvar="FORECAST_REGION",   default="india")
@click.option("--bucket",         envvar="GCS_BUCKET",        required=True)
@click.option("--weights-bucket", envvar="GCS_WEIGHTS_BUCKET", required=True)
def main(aifs_date, region, bucket, weights_bucket):
    # date is the NeuralGCM date; AIFS IC is 12 hours earlier

    logger.info(f"IC date: {aifs_date}")

    _setup_directories()
    _download_inputs(aifs_date, region, bucket, weights_bucket)
    _run_science_scripts(aifs_date)
    _upload_outputs(aifs_date, region, bucket)


def _setup_directories() -> None:
    for subdir in ["raw/ifs_ic", "raw/output", "output/sji", "output/tcw", "output/tp",
                   "output/tp_0p25", "weights", "EKR/mir_16_linear", "data", "grids"]:
        (AIFS_UTILS.parent / subdir).mkdir(parents=True, exist_ok=True)


def _download_inputs(aifs_date: str, region: str, bucket: str, weights_bucket: str) -> None:
    # 1. ECMWF initial conditions pickle
    # The downloader wrote the actual ECMWF date to GCS; read it to find the right file.
    # If the recorded ecmwf date doesn't match aifs_date, use whatever the downloader produced.
    try:
        ecmwf_date = read_gcs_text(bucket, f"{region}/intermediate/latest_ecmwf_date.txt")
        logger.info(f"Recorded ECMWF date: {ecmwf_date}")
    except Exception:
        ecmwf_date = aifs_date
        logger.warning(f"Could not read latest_ecmwf_date.txt; assuming ecmwf_date={ecmwf_date}")

    download_gcs_file(
        bucket,
        f"{region}/raw/ecmwf/{ecmwf_date}/input_state_{ecmwf_date}.pkl",
        AIFS_UTILS.parent / "raw" / "ifs_ic" / f"input_state_{ecmwf_date}.pkl",
    )

    # Symlink or rename to the aifs_date name if they differ, so run_model finds it
    ic_ecmwf = AIFS_UTILS.parent / "raw" / "ifs_ic" / f"input_state_{ecmwf_date}.pkl"
    ic_aifs  = AIFS_UTILS.parent / "raw" / "ifs_ic" / f"input_state_{aifs_date}.pkl"
    if ecmwf_date != aifs_date and not ic_aifs.exists():
        ic_aifs.symlink_to(ic_ecmwf)

    # 2. Model weights
    download_gcs_file(
        weights_bucket,
        "aifs/aifs-single-mse-1.1.ckpt",
        AIFS_UTILS.parent / "weights" / "aifs-single-mse-1.1.ckpt",
    )

    # 3. Sparse transform matrix (filename hardcoded in download_ic.py / run_model.py)
    sparse = "9533e90f8433424400ab53c7fafc87ba1a04453093311c0b5bd0b35fedc1fb83.npz"
    download_gcs_file(
        weights_bucket,
        f"aifs/EKR/mir_16_linear/{sparse}",
        AIFS_UTILS.parent / "EKR" / "mir_16_linear" / sparse,
    )

    # 4. Post processing data files (e.g. lat/lon grids, masks)
    grid_2p0 = "grid_2p0.txt"
    download_gcs_file(
        weights_bucket,
        f"aifs/grids/{grid_2p0}",
        AIFS_UTILS.parent / "grids" / grid_2p0,
    )

    india_mask = "india_mask_2p0.nc"
    download_gcs_file(
        weights_bucket,
        f"aifs/data/{india_mask}",
        AIFS_UTILS.parent / "data" / india_mask,
    )

def _run_science_scripts(aifs_date: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(AIFS_UTILS)}

    logger.info(f"Running AIFS run_model.py for {aifs_date}")
    subprocess.run(
        [sys.executable, "run_model.py", "--date", aifs_date],
        cwd=AIFS_UTILS, check=True, env=env,
    )

    logger.info(f"Running AIFS post_process.py for {aifs_date}")
    subprocess.run(
        [sys.executable, "post_process.py", "--date", aifs_date],
        cwd=AIFS_UTILS, check=True, env=env,
    )


def _upload_outputs(aifs_date: str, region: str, bucket: str) -> None:
    output_dir = AIFS_UTILS.parent / "output"
    gcs_prefix = f"{region}/output/aifs/{aifs_date}"
    upload_directory(bucket, output_dir, gcs_prefix)
    logger.info(f"AIFS outputs uploaded to gs://{bucket}/{gcs_prefix}/")


if __name__ == "__main__":
    main()
