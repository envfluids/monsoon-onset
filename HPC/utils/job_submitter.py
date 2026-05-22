import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 5
BACKOFF_FACTOR = 2
SLURM_CLUSTERS = {"dsi", "midway"}
PBS_CLUSTERS = {"derecho"}


@dataclass(frozen=True)
class ClusterConfig:
    name: str
    script_dir: Path
    project_root: Path


def project_root():
    return Path(__file__).resolve().parents[2]


def get_cluster():
    root = project_root()
    config_file = root / ".config" / "config.json"
    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    cluster = config["cluster"]
    logging.info("Cluster: %s", cluster)
    return ClusterConfig(
        name=cluster,
        script_dir=root / "HPC" / cluster,
        project_root=root,
    )


def build_command(cluster, label, script_path, log_dir, date_f):
    script_path = Path(script_path).resolve()
    log_dir = Path(log_dir).resolve()

    job_name = f"{label}_{date_f}"

    if cluster in SLURM_CLUSTERS:
        return [
            "sbatch",
            f"--job-name={job_name}",
            f"--output={log_dir / (job_name + '.o%j')}",
            f"--error={log_dir / (job_name + '.e%j')}",
            f"--export=DATE_F={date_f},MODEL={label}",
            str(script_path),
        ]

    if cluster in PBS_CLUSTERS:
        return [
            "qsub",
            "-N",
            job_name,
            "-o",
            str(log_dir / f"{job_name}.out"),
            "-e",
            str(log_dir / f"{job_name}.err"),
            "-v",
            f"DATE_F={date_f}",
            str(script_path),
        ]

    raise ValueError(f"Unknown cluster: {cluster}. Exiting.")


def command_to_string(command):
    return " ".join(str(part) for part in command)


def parse_job_id(cluster, returncode, stdout):
    stdout = stdout.strip()
    if cluster in SLURM_CLUSTERS and returncode == 0:
        match = re.search(r"Submitted batch job (\d+)", stdout)
        if match:
            return match.group(1)

    if cluster in PBS_CLUSTERS and returncode == 0:
        if re.match(r"^\d+\.[a-zA-Z0-9._-]+$", stdout):
            return stdout

    return None


def submit_job(command, cluster, label, cwd=None, dry_run=False):
    command_str = command_to_string(command)
    if dry_run:
        logging.info("Dry run: would submit %s from %s", command_str, cwd or Path.cwd())
        return None

    job_id_str = None
    submission_successful = False

    for attempt in range(MAX_RETRIES):
        logging.info("Attempt %s of %s to submit job: %s", attempt + 1, MAX_RETRIES, label)
        logging.debug("Executing command: %s", command_str)
        try:
            process = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            stdout_str = process.stdout.strip()
            stderr_str = process.stderr.strip()

            logging.debug("Attempt %s - Return code: %s", attempt + 1, process.returncode)
            logging.debug("Attempt %s - Stdout: %s", attempt + 1, stdout_str)
            logging.debug("Attempt %s - Stderr: %s", attempt + 1, stderr_str)

            current_attempt_job_id = parse_job_id(cluster, process.returncode, stdout_str)
            if current_attempt_job_id:
                job_id_str = current_attempt_job_id
                submission_successful = True
                logging.info(
                    "Successfully submitted job %s for %s on attempt %s on %s.",
                    job_id_str,
                    label,
                    attempt + 1,
                    cluster,
                )
                if cluster in SLURM_CLUSTERS and stderr_str:
                    logging.info("Slurm stderr (may contain verification info): %s", stderr_str)
                elif cluster in PBS_CLUSTERS and stderr_str:
                    logging.warning(
                        "PBS job %s submitted, but stderr was not empty: '%s'.",
                        job_id_str,
                        stderr_str,
                    )
                break

            logging.warning("Job submission failed on attempt %s for %s.", attempt + 1, label)
            logging.warning("RC: %s, Stdout: '%s', Stderr: '%s'", process.returncode, stdout_str, stderr_str)
            if attempt < MAX_RETRIES - 1:
                current_delay = RETRY_DELAY_SECONDS * (BACKOFF_FACTOR ** attempt)
                logging.info("Waiting %s seconds before next attempt...", current_delay)
                time.sleep(current_delay)
            else:
                logging.error("Job %s submission failed after %s attempts.", label, MAX_RETRIES)
                logging.error("Last command executed: %s", command_str)
                logging.error("Last stdout: %s", stdout_str)
                logging.error("Last stderr: %s", stderr_str)

        except Exception as exc:
            logging.error(
                "A Python exception occurred during submission attempt %s for %s: %s",
                attempt + 1,
                label,
                exc,
            )
            if attempt < MAX_RETRIES - 1:
                current_delay = RETRY_DELAY_SECONDS * (BACKOFF_FACTOR ** attempt)
                logging.info("Waiting %s seconds before next attempt due to script error...", current_delay)
                time.sleep(current_delay)
            else:
                logging.error("Job %s submission failed after %s attempts.", label, MAX_RETRIES)
                break

    if submission_successful and job_id_str:
        logging.info("The job %s has been queued on %s.", job_id_str, cluster)
    else:
        logging.error("Ultimately failed to submit job %s to %s after %s attempts.", label, cluster, MAX_RETRIES)

    return job_id_str
