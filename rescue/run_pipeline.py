#!/glade/u/apps/opt/conda/bin/python
#PBS -A uric0009
#PBS -l select=1:ncpus=32:ngpus=1:mem=100GB
#PBS -l walltime=01:00:00
#PBS -q develop
#PBS -j oe


import subprocess
from pathlib import Path
import argparse
import logging
from datetime import datetime, timedelta
import os
os.system("ml conda")
# --- Basic Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
    handlers=[logging.StreamHandler(), logging.FileHandler("pipeline_recovery_derecho.log")],
)

# --- Derecho Environment Configuration ---
# Maps pipeline steps to the full paths of the required conda environments
# as found in the *_derecho.sh scripts.
CONDA_ENV_PATHS = {
    "aifs_run": "/glade/work/marchakitus/conda-envs/AIFSv1",
    "aifs_postprocess": "/glade/work/marchakitus/conda-envs/ncl_stable",
    "ngcm_preprocess": "/glade/work/marchakitus/conda-envs/ncl_stable",
    "ngcm_run": "/glade/work/marchakitus/conda-envs/neuralgcm",
    "ngcm_postprocess": "/glade/work/marchakitus/conda-envs/ncl_stable",
    "ngcm_merge": "/glade/work/marchakitus/conda-envs/neuralgcm",
    "ngcm_google_postprocess": "/glade/work/marchakitus/conda-envs/ncl_stable",
    "blend": "/glade/work/marchakitus/conda-envs/ncl_stable",
    "sync": "/glade/work/marchakitus/conda-envs/monsoon",
    "s2s": "/glade/work/marchakitus/conda-envs/S2S",
}


def run_command(command, cwd, env_path):
    """
    Runs a command in a specified directory using 'conda run --prefix'.

    Args:
        command (list): The command and its arguments as a list of strings.
        cwd (Path): The working directory to run the command in.
        env_path (str): The full path to the conda environment.

    Returns:
        bool: True if the command succeeded, False otherwise.
    """
    full_command = ["conda", "run", "--prefix", env_path] + command
    logging.info(f"Running in '{cwd}' with env at '{env_path}': {' '.join(full_command)}")
    try:
        result = subprocess.run(
            full_command,
            check=True,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=3600,  # 60-minute timeout for potentially longer runs on Derecho
        )
        logging.info(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            logging.warning(f"STDERR:\n{result.stderr}")
        return True
    except subprocess.TimeoutExpired as e:
        logging.error(f"Command timed out after 60 minutes: {' '.join(full_command)}")
        logging.error(f"STDOUT: {e.stdout}")
        logging.error(f"STDERR: {e.stderr}")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {' '.join(full_command)}")
        logging.error(f"Return code: {e.returncode}")
        logging.error(f"STDOUT:\n{e.stdout}")
        logging.error(f"STDERR:\n{e.stderr}")
        return False
    except FileNotFoundError:
        logging.error(
            f"Command 'conda' not found. Please ensure conda is initialized and in your system's PATH."
        )
        return False


def check_aifs_pipeline(base_path, main_date):
    """
    Checks and runs the AIFS pipeline for a given date.
    Note: AIFS data is expected to be for 12 hours prior to the main date.
    """
    logging.info("--- Checking AIFS Pipeline ---")

    # The blend script expects AIFS data from 12 hours prior.
    try:
        aifs_date_obj = datetime.strptime(main_date, "%Y%m%dT%H") - timedelta(hours=12)
        aifs_date = aifs_date_obj.strftime("%Y%m%dT%H")
    except ValueError:
        logging.error(f"Invalid date format for main_date: {main_date}")
        return False

    logging.info(f"AIFS operations will use adjusted date: {aifs_date}")

    aifs_path = base_path / "AIFS"
    aifs_utils_path = aifs_path / "utils"
    aifs_output_tp = aifs_path / "output" / "tp" / f"tp_{aifs_date}.nc"
    aifs_raw_output = aifs_path / "raw" / "output" / f"init_{aifs_date}.nc"
    aifs_ic_file = aifs_path / "raw" / "ifs_ic" / f"input_state_{aifs_date}.pkl"

    if aifs_output_tp.exists():
        logging.info(f"Final AIFS output {aifs_output_tp} exists. Skipping.")
        return True

    # Check for raw model output
    if not aifs_raw_output.exists():
        logging.info(f"AIFS raw model output missing for {aifs_date}.")
        if not aifs_ic_file.exists():
            logging.error(
                f"AIFS IC file {aifs_ic_file} missing. Cannot proceed. Please ensure this file exists."
            )
            return False

        logging.info("Running AIFS model.")
        cmd = ["python", "run_model.py", "--date", aifs_date]
        if not run_command(cmd, cwd=aifs_utils_path, env_path=CONDA_ENV_PATHS["aifs_run"]):
            return False

    logging.info(f"Running AIFS post-processing for {aifs_date}.")
    cmd = ["python", "post_process.py", "--date", aifs_date]
    if not run_command(
        cmd, cwd=aifs_utils_path, env_path=CONDA_ENV_PATHS["aifs_postprocess"]
    ):
        return False

    logging.info("AIFS pipeline check successful.")
    return True


def check_neuralgcm_pipeline(base_path, date):
    """Checks and runs the self-hosted NeuralGCM pipeline."""
    logging.info("--- Checking NeuralGCM Pipeline ---")
    ngcm_path = base_path / "NeuralGCM"
    ngcm_utils_path = ngcm_path / "utils"
    ngcm_output_tp = ngcm_path / "output" / "tp" / f"tp_{date}.nc"
    ngcm_processed_ic = ngcm_path / "raw" / "ncep_ic" / "processed" / f"gdas_{date}.nc"
    ngcm_downloaded_ic = (
        ngcm_path / "raw" / "ncep_ic" / "download" / f"gdas_{date}.pgrb2"
    )
    ngcm_raw_output_dir = ngcm_path / "raw" / "output" / date

    if ngcm_output_tp.exists():
        logging.info(f"Final NeuralGCM output {ngcm_output_tp} exists. Skipping.")
        return True

    # Check backwards from merge to download
    if not any((ngcm_path / "output" / "tcw").glob(f"*{date}*")) and not any(
        (ngcm_path / "output" / "tp").glob(f"*{date}*")
    ):
        if not ngcm_raw_output_dir.exists() or not any(
            ngcm_raw_output_dir.glob("*.zarr")
        ):
            logging.info(f"NeuralGCM raw Zarr output missing for {date}.")
            if not ngcm_processed_ic.exists():
                logging.info(f"NeuralGCM processed IC missing for {date}.")
                if not ngcm_downloaded_ic.exists():
                    logging.error(
                        f"NeuralGCM downloaded IC {ngcm_downloaded_ic} missing. Cannot proceed."
                    )
                    return False
                logging.info(f"Running NeuralGCM preprocessing for {date}.")
                cmd = ["python", "preprocess.py", "--date", date]
                if not run_command(
                    cmd, cwd=ngcm_utils_path, env_path=CONDA_ENV_PATHS["ngcm_preprocess"]
                ):
                    return False
            logging.info(f"Running NeuralGCM model for {date}.")
            cmd = ["python", "run_model.py", "--date", date]
            if not run_command(
                cmd, cwd=ngcm_utils_path, env_path=CONDA_ENV_PATHS["ngcm_run"]
            ):
                return False

        logging.info(f"Running NeuralGCM post-processing (members) for {date}.")
        cmd = ["python", "post_process.py", "--date", date]
        if not run_command(
            cmd, cwd=ngcm_utils_path, env_path=CONDA_ENV_PATHS["ngcm_postprocess"]
        ):
            return False

        logging.info(f"Running NeuralGCM post-processing (merge) for {date}.")
        cmd = ["python", "post_process_merge.py", "--date", date]
        if not run_command(
            cmd, cwd=ngcm_utils_path, env_path=CONDA_ENV_PATHS["ngcm_merge"]
        ):
            return False

    logging.info("NeuralGCM pipeline check successful.")
    return True


def check_neuralgcm_google_pipeline(base_path, date):
    """Checks and runs the NeuralGCM-Google pipeline."""
    logging.info("--- Checking NeuralGCM-Google Pipeline ---")
    ngcm_g_path = base_path / "NeuralGCM_google"
    ngcm_g_utils_path = ngcm_g_path / "utils"
    ngcm_g_output_tp = ngcm_g_path / "output" / "tp" / f"tp_{date}.nc"
    ngcm_g_raw_file = ngcm_g_path / "raw" / f"{date}.nc"

    if ngcm_g_output_tp.exists():
        logging.info(f"Final NeuralGCM-Google output {ngcm_g_output_tp} exists. Skipping.")
        return True

    if not ngcm_g_raw_file.exists():
        logging.error(
            f"NeuralGCM-Google raw file {ngcm_g_raw_file} missing. Cannot proceed."
        )
        return False

    logging.info(f"Running NeuralGCM-Google post-processing for {date}.")
    cmd = ["python", "post_process.py", "--date", date]
    if not run_command(
        cmd, cwd=ngcm_g_utils_path, env_path=CONDA_ENV_PATHS["ngcm_google_postprocess"]
    ):
        return False

    logging.info("NeuralGCM-Google pipeline check successful.")
    return True


def run_blend_pipeline(base_path, date, source=None):
    """Runs the blending and visualization pipeline."""
    logging.info(f"--- Running Blend Pipeline (source: {source or 'default'}) ---")
    blend_utils_path = base_path / "blend" / "utils"
    output_dir_name = "output_google" if source == "google" else "output"
    blend_summary_file = base_path / "blend" / output_dir_name / date / "blend_output_summary.csv"

    if blend_summary_file.exists():
        logging.info(f"Blend output {blend_summary_file} exists. Skipping blend.")
        return True
    
    cmd = ["python", "main.py", "--date", date]
    if source:
        cmd.extend(["--source", source])

    if not run_command(cmd, cwd=blend_utils_path, env_path=CONDA_ENV_PATHS["blend"]):
        return False

    logging.info("Blend pipeline successful.")
    return True


def run_sync_pipeline(base_path):
    """Triggers the sync pipeline to update operational repo and Google Drive."""
    logging.info("--- Triggering Sync Pipeline ---")
    sync_utils_path = base_path / "sync" / "utils"
    cmd = ["python", "main.py"]
    if not run_command(cmd, cwd=sync_utils_path, env_path=CONDA_ENV_PATHS["sync"]):
        return False
    logging.info("Sync pipeline triggered successfully.")
    return True


def run_s2s_pipeline(base_path):
    """Triggers the S2S pipeline."""
    logging.info("--- Triggering S2S Pipeline ---")
    s2s_utils_path = base_path / "S2S" / "utils"
    cmd = ["python", "pipeline.py"]
    if not run_command(cmd, cwd=s2s_utils_path, env_path=CONDA_ENV_PATHS["s2s"]):
        return False
    logging.info("S2S pipeline triggered successfully.")
    return True


def main():
    """Main function to orchestrate the end-to-end pipeline recovery."""
    parser = argparse.ArgumentParser(
        description="End-to-end monsoon onset pipeline recovery and execution script for the Derecho cluster."
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="The primary forecast date in YYYYMMDDTHH format (e.g., 20250615T12).",
    )
    args = parser.parse_args()
    date = args.date
    
    base_path = Path.cwd()

    logging.warning(
        "This script attempts to run the pipeline for a specific date. "
        "It assumes that the required input data (Initial Conditions) for the specified date "
        "already exists on disk, as the downloader scripts typically fetch the latest data."
    )

    # Step 1: Ensure input data pipelines are complete
    aifs_ok = check_aifs_pipeline(base_path, date)
    ngcm_ok = check_neuralgcm_pipeline(base_path, date)
    ngcm_google_ok = check_neuralgcm_google_pipeline(base_path, date)

    # Step 2: Run Blending for different sources
    if aifs_ok and ngcm_ok:
        run_blend_pipeline(base_path, date, source=None)
    else:
        logging.error("Skipping AIFS+NeuralGCM blend due to upstream failures.")

    if aifs_ok and ngcm_google_ok:
        run_blend_pipeline(base_path, date, source="google")
    else:
        logging.error("Skipping AIFS+NeuralGCM-Google blend due to upstream failures.")

    # Step 3: Run the Sync process
    run_sync_pipeline(base_path)

    # Step 4: Trigger the S2S pipeline
    run_s2s_pipeline(base_path)

    logging.info("--- End-to-end script has finished its execution. ---")


if __name__ == "__main__":
    main()
