from download_ncep import get_data
import os
import logging
import json
from pathlib import Path
import subprocess
import time
import re

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

SCRIPT_BASE = "run_model"

ALLOWED_HOURS = ["00", "12"]
MAX_RETRIES = 5  # Max number of submission attempts
RETRY_DELAY_SECONDS = 5  # Initial wait time in seconds between retries
BACKOFF_FACTOR = 2 # Factor by which the delay increases


def get_cluster():
    base = Path(__file__).resolve().parent.parent.parent
    config_file = base / ".config" / "config.json"
    with open(config_file, "r") as f:
        config = json.load(f)
    cluster = config["cluster"]
    logging.info(f"Cluster: {cluster}")
    return cluster


def main():
    DATE_F = get_data()
    # DATE_F = "20250523T00" # Uncomment for testing a specific date

    if DATE_F:
        hour = DATE_F.split("T")[-1]
        if hour in ALLOWED_HOURS:
            logging.info("IC download script was successful, new data available")
            logging.info(f"Initializing compute job for date: {DATE_F}")
            cluster = get_cluster()
            JOB_NAME = f"NGCM_fc_{DATE_F}"
            if cluster == "midway":
                logging.info("Midway cluster is no longer supported")
                return 
                command = (
                    f"sbatch "
                    f"--job-name={JOB_NAME} "
                    f"--output=../logs/{JOB_NAME}.o%j "
                    f"--error=../logs/{JOB_NAME}.e%j "
                    f"--export=DATE_F={DATE_F} "
                    f"{SCRIPT_BASE}_{cluster}.sh"
                )
            elif cluster == "derecho":
                command = (
                    f"qsub "
                    f"-N {JOB_NAME} "
                    f"-o ../logs/{JOB_NAME}.out "
                    f"-e ../logs/{JOB_NAME}.err "
                    f"-v DATE_F={DATE_F} "
                    f"{SCRIPT_BASE}_{cluster}.sh"
                )
            else:
                raise ValueError(f"Unknown cluster: {cluster}. Exiting.")

            job_id_str = None
            submission_successful = False

            for attempt in range(MAX_RETRIES):
                logging.info(f"Attempt {attempt + 1} of {MAX_RETRIES} to submit job: {JOB_NAME}")
                logging.debug(f"Executing command: {command}")
                try:
                    process = subprocess.run(
                        command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    stdout_str = process.stdout.strip()
                    stderr_str = process.stderr.strip()

                    logging.debug(f"Attempt {attempt + 1} - Return code: {process.returncode}")
                    logging.debug(f"Attempt {attempt + 1} - Stdout: {stdout_str}")
                    logging.debug(f"Attempt {attempt + 1} - Stderr: {stderr_str}")

                    current_attempt_job_id = None

                    if cluster == "midway":
                        if process.returncode == 0 and "Submitted batch job" in stdout_str:
                            match = re.search(r"Submitted batch job (\d+)", stdout_str)
                            if match:
                                current_attempt_job_id = match.group(1)
                    elif cluster == "derecho":
                        if process.returncode == 0 and re.match(r"^\d+\.[a-zA-Z0-9._-]+$", stdout_str):
                            current_attempt_job_id = stdout_str

                    if current_attempt_job_id:
                        job_id_str = current_attempt_job_id
                        submission_successful = True
                        logging.info(f"Successfully submitted job {job_id_str} for {JOB_NAME} on attempt {attempt + 1} on {cluster}.")
                        if cluster == "midway" and stderr_str:
                             logging.info(f"Slurm stderr (may contain verification info): {stderr_str}")
                        elif cluster == "derecho" and stderr_str:
                             logging.warning(f"PBS job {job_id_str} submitted, but stderr was not empty: '{stderr_str}'. Proceeding as job ID was obtained.")
                        break
                    else:
                        logging.warning(f"Job submission failed on attempt {attempt + 1} for {JOB_NAME}.")
                        logging.warning(f"RC: {process.returncode}, Stdout: '{stdout_str}', Stderr: '{stderr_str}'")
                        
                        if attempt < MAX_RETRIES - 1:
                            current_delay = RETRY_DELAY_SECONDS * (BACKOFF_FACTOR ** attempt)
                            logging.info(f"Waiting {current_delay} seconds before next attempt (attempt {attempt + 1} failed, next is {attempt + 2})...")
                            time.sleep(current_delay)
                        else:
                            logging.error(f"Job {JOB_NAME} submission failed after {MAX_RETRIES} attempts.")
                            logging.error(f"Last command executed: {command}")
                            logging.error(f"Last stdout: {stdout_str}")
                            logging.error(f"Last stderr: {stderr_str}")
                
                except Exception as e:
                    logging.error(f"A Python exception occurred during submission attempt {attempt + 1} for {JOB_NAME}: {e}")
                    if attempt < MAX_RETRIES - 1:
                        current_delay = RETRY_DELAY_SECONDS * (BACKOFF_FACTOR ** attempt)
                        logging.info(f"Waiting {current_delay} seconds before next attempt due to script error (attempt {attempt + 1} failed, next is {attempt + 2})...")
                        time.sleep(current_delay)
                    else:
                        logging.error(f"Job {JOB_NAME} submission failed after {MAX_RETRIES} attempts, with the last attempt failing due to a script error.")
                        break

            if submission_successful and job_id_str:
                logging.info(f"The job {job_id_str} to run the model has been queued on {cluster}.")
            else:
                logging.error(f"Ultimately failed to submit job {JOB_NAME} to {cluster} after {MAX_RETRIES} attempts.")

        else:
            logging.info(f"New data available for {DATE_F}, but hour {hour} is not in allowed hours: {ALLOWED_HOURS}")
            logging.info("Exiting model run pipeline for this cycle.")

    else:
        logging.info(
            "Will not attempt to run model, as no new data was downloaded."
        )


if __name__ == "__main__":
    main()
