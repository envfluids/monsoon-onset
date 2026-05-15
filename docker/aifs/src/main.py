"""
Monsoon AIFS — GCS Shim Wrapper

Downloads ECMWF GRIB initial conditions and model weights from GCS, runs AIFS
or AIFS-ENS inference and post-processing using the original unmodified science
scripts, then uploads outputs back to GCS.

The science scripts (run_model.py, post_process.py) use relative paths from
their utils/ directory, so we set cwd=/app/AIFS/utils/ before invoking them.

Environment Variables:
    DATE               : Forecast date YYYYMMDDTHH
    AIFS_MODEL         : AIFS, AIFS_ENS, or both (default: AIFS)
    FORECAST_REGION    : e.g. 'india'
    GCS_BUCKET         : Region data bucket for post-processed outputs
    GCS_COMMON_BUCKET  : Common data bucket for ICs and full-field raw forecasts
    GCS_WEIGHTS_BUCKET : Weights/static-files bucket
"""

import os
import sys
import logging
import subprocess
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
SPARSE_RUN_NAME = "7f0be51c7c1f522592c7639e0d3f95bcbff8a044292aa281c1e73b842736d9bf.npz"

MODEL_WEIGHT_PATHS = {
    "AIFS": ("aifs/aifs-single-mse-1.1.ckpt", "aifs-single-mse-1.1.ckpt"),
    "AIFS_ENS": ("aifs/aifs-ens-crps-1.0.ckpt", "aifs-ens-crps-1.0.ckpt"),
}


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


def upload_file(bucket_name: str, local_path: Path, gcs_path: str) -> None:
    _client().bucket(bucket_name).blob(gcs_path).upload_from_filename(str(local_path))
    logger.info(f"Uploaded {local_path} → gs://{bucket_name}/{gcs_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--date",           envvar="DATE",              required=True)
@click.option("--model",          envvar="AIFS_MODEL",        default="AIFS",
              type=click.Choice(["AIFS", "AIFS_ENS", "both"], case_sensitive=False))
@click.option("--region",         envvar="FORECAST_REGION",   default="india")
@click.option("--bucket",         envvar="GCS_BUCKET",        required=True)
@click.option("--weights-bucket", envvar="GCS_WEIGHTS_BUCKET", required=True)
def main(date, model, region, bucket, weights_bucket):
    common_bucket = os.environ.get("GCS_COMMON_BUCKET", bucket)
    model = model.upper()
    if model == "BOTH":
        model = "both"
    logger.info(f"IC date: {date}")
    logger.info(f"AIFS model selection: {model}")

    _setup_directories()
    aifs_date = _download_inputs(date, region, common_bucket, weights_bucket, _models_to_run(model))
    for model_name in _models_to_run(model):
        _run_science_scripts(aifs_date, model_name)
        _upload_outputs(aifs_date, model_name, region, bucket, common_bucket)


def _setup_directories() -> None:
    for subdir in [
        "raw/ifs_ic/grib",
        "raw/output/AIFS",
        "raw/output/AIFS_ENS",
        "output/india/sji",
        "output/india/tcw",
        "output/india/tp",
        "output/ethiopia/AIFS/tp",
        "output/ethiopia/AIFS_ENS/tp",
        "weights",
        "EKR/mir_16_linear",
        "data",
        "grids",
    ]:
        (AIFS_UTILS.parent / subdir).mkdir(parents=True, exist_ok=True)


def _models_to_run(model: str) -> list[str]:
    if model == "both":
        return ["AIFS", "AIFS_ENS"]
    return [model]


def _download_inputs(
    aifs_date: str,
    region: str,
    bucket: str,
    weights_bucket: str,
    models: list[str],
) -> str:
    # 1. ECMWF GRIB initial conditions
    # The downloader wrote the actual ECMWF date to GCS; read it to find the right file.
    try:
        ecmwf_date = read_gcs_text(bucket, "intermediate/latest_ecmwf_date.txt")
        logger.info(f"Recorded ECMWF date: {ecmwf_date}")
    except Exception:
        ecmwf_date = aifs_date
        logger.warning(f"Could not read latest_ecmwf_date.txt; assuming ecmwf_date={ecmwf_date}")

    _download_ecmwf_gribs(ecmwf_date, region, bucket)

    # 2. Model weights
    for model in models:
        gcs_path, filename = MODEL_WEIGHT_PATHS[model]
        download_gcs_file(
            weights_bucket,
            gcs_path,
            AIFS_UTILS.parent / "weights" / filename,
        )

    # 3. Sparse transform matrices used by preprocess_ic.py and run_model*.py
    for sparse in [SPARSE_DOWNLOAD_NAME, SPARSE_RUN_NAME]:
        download_gcs_file(
            weights_bucket,
            f"aifs/EKR/mir_16_linear/{sparse}",
            AIFS_UTILS.parent / "EKR" / "mir_16_linear" / sparse,
        )

    return ecmwf_date


def _download_ecmwf_gribs(date_str: str, region: str, bucket: str) -> None:
    for filename in _expected_ecmwf_grib_names(date_str):
        download_gcs_file(
            bucket,
            f"raw/ecmwf/{date_str}/grib/{filename}",
            AIFS_UTILS.parent / "raw" / "ifs_ic" / "grib" / filename,
        )


def _expected_ecmwf_grib_names(date_str: str) -> list[str]:
    date = datetime.strptime(date_str, "%Y%m%dT%H")
    dates = [date - timedelta(hours=12), date - timedelta(hours=6), date]
    return [d.strftime("%Y%m%d%H0000-0h-oper-fc.grib2") for d in dates]


def _run_science_scripts(aifs_date: str, model: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(AIFS_UTILS)}
    run_script = "run_model.py" if model == "AIFS" else "run_model_ENS.py"

    logger.info(f"Running {model} {run_script} for {aifs_date}")
    subprocess.run(
        [sys.executable, run_script, "--date", aifs_date],
        cwd=AIFS_UTILS, check=True, env=env,
    )

    logger.info(f"Running AIFS post_process.py for {model} {aifs_date}")
    subprocess.run(
        [sys.executable, "post_process.py", "--date", aifs_date, "--model", model],
        cwd=AIFS_UTILS, check=True, env=env,
    )


def _upload_outputs(
    aifs_date: str,
    model: str,
    region: str,
    bucket: str,
    common_bucket: str,
) -> None:
    _upload_raw_forecast(aifs_date, model, common_bucket)
    _upload_postprocessed_outputs(aifs_date, model, region, bucket)


def _upload_raw_forecast(aifs_date: str, model: str, bucket: str) -> None:
    model_prefix = "aifs" if model == "AIFS" else "aifs_ens"
    raw_dir = AIFS_UTILS.parent / "raw" / "output" / model
    raw_name = f"init_{aifs_date}.nc" if model == "AIFS" else f"init_{aifs_date}.zarr"
    raw_path = raw_dir / raw_name
    if raw_path.is_dir():
        upload_directory(
            bucket,
            raw_path,
            f"raw_forecast/{model_prefix}/{aifs_date}/{raw_name}",
        )
    elif raw_path.is_file():
        upload_file(
            bucket,
            raw_path,
            f"raw_forecast/{model_prefix}/{aifs_date}/{raw_name}",
        )
    else:
        raise RuntimeError(f"Expected raw forecast output is missing: {raw_path}")


def _upload_postprocessed_outputs(aifs_date: str, model: str, region: str, bucket: str) -> None:
    model_prefix = "aifs" if model == "AIFS" else "aifs_ens"
    for local_dir, gcs_prefix in _region_output_prefixes(aifs_date, model, region):
        if local_dir.exists():
            upload_directory(bucket, local_dir, gcs_prefix)
        else:
            logger.warning("Region output directory is missing: %s", local_dir)
    _upload_blend_ready_aliases(aifs_date, model, region, bucket)
    logger.info("%s post-processed outputs uploaded for region=%s", model, region)


def _region_output_prefixes(aifs_date: str, model: str, region: str) -> list[tuple[Path, str]]:
    output_dir = AIFS_UTILS.parent / "output"
    model_prefix = "aifs" if model == "AIFS" else "aifs_ens"
    if model == "AIFS" and region == "india":
        return [(output_dir / "india", f"output/{model_prefix}/{aifs_date}/india")]
    if model == "AIFS" and region == "ethiopia":
        return [(output_dir / "ethiopia" / "AIFS", f"output/{model_prefix}/{aifs_date}/ethiopia/AIFS")]
    if model == "AIFS_ENS" and region == "ethiopia":
        return [(output_dir / "ethiopia" / "AIFS_ENS", f"output/{model_prefix}/{aifs_date}/ethiopia/AIFS_ENS")]
    logger.info("No %s post-processed outputs are configured for region=%s", model, region)
    return []


def _upload_blend_ready_aliases(aifs_date: str, model: str, region: str, bucket: str) -> None:
    model_prefix = "aifs" if model == "AIFS" else "aifs_ens"
    alias_prefix = f"output/{model_prefix}/{aifs_date}"
    if model == "AIFS" and region == "india":
        aliases = {
            AIFS_UTILS.parent / "output" / "india" / "sji" / f"sji_{aifs_date}.nc":
                f"{alias_prefix}/sji/sji_{aifs_date}.nc",
            AIFS_UTILS.parent / "output" / "india" / "tcw" / f"tcw_{aifs_date}.nc":
                f"{alias_prefix}/tcw/tcw_{aifs_date}.nc",
            AIFS_UTILS.parent / "output" / "india" / "tp" / f"tp_0p25_{aifs_date}.nc":
                f"{alias_prefix}/tp/tp_{aifs_date}.nc",
        }
    elif model == "AIFS_ENS" and region == "ethiopia":
        aliases = {
            AIFS_UTILS.parent / "output" / "ethiopia" / "AIFS_ENS" / "tp" / f"tp_0p25_{aifs_date}.nc":
                f"{alias_prefix}/tp/tp_{aifs_date}.nc",
        }
    else:
        aliases = {}

    for local_path, gcs_path in aliases.items():
        if local_path.exists():
            upload_file(bucket, local_path, gcs_path)
        else:
            logger.warning("Blend-ready alias source is missing: %s", local_path)


if __name__ == "__main__":
    main()
