from download_ncep import get_data
import os
import logging
import json
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
ALLOWED_HOURS = ["00", "12"]


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
    # DATE_F = "20250511T12"
    if DATE_F:
        hour = DATE_F.split("T")[-1]
        if hour in ALLOWED_HOURS:
            logging.info("IC download script was successful, new data available")
            logging.info(f"Initializing compute job for date: {DATE_F}")
            cluster = get_cluster()

            if cluster == "midway":
                command = (
                    f"sbatch "
                    f"--job-name=NGCM_fc_{DATE_F} "
                    f"--output=../logs/NGCM_fc_{DATE_F}.o%j "
                    f"--error=../logs/NGCM_fc_{DATE_F}.e%j "
                    f"--export=DATE_F={DATE_F} "
                    f"run_model_{cluster}.sh"
                )
            elif cluster == "derecho":
                command = (
                    f"qsub "
                    f"-N NGCM_fc_{DATE_F} "
                    f"-o ../logs/NGCM_fc_{DATE_F}.out "
                    f"-e ../logs/NGCM_fc_{DATE_F}.err "
                    f"-v DATE_F={DATE_F} "
                    f"run_model_{cluster}.sh"
                )
            else:
                raise ValueError(f"Unknown cluster: {cluster}. Exiting.")

            os.system(command)
            logging.info("Running model")

        else:
            logging.info(f"New data available, but hour {hour} not in {ALLOWED_HOURS}")
            logging.info("Exiting pipeline")

    else:
        logging.info(
            "Will not run model, no new data to download. Retrying in 30 minutes"
        )


if __name__ == "__main__":
    main()
