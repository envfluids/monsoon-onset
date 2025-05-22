from download_imerg import get_data
import os
import logging
import json
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

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
    # DATE_F = "20250510"
    # print(DATE_F)
    if DATE_F:
        logging.info("IMERG download script was successful, new data available")
        logging.info(f"Initializing compute job for date: {DATE_F}")
        cluster = get_cluster()

        if cluster == "midway":
            command = (
                f"sbatch "
                f"--job-name=IMERG_{DATE_F} "
                f"--output=../logs/IMERG_{DATE_F}.o%j "
                f"--error=../logs/IMERG_{DATE_F}.e%j "
                f"--export=DATE_F={DATE_F} "
                f"process_data_{cluster}.sh"
            )
        elif cluster == "derecho":
            command = (
                f"sbatch "
                f"--job-name=IMERG_{DATE_F} "
                f"--output=../logs/IMERG_{DATE_F}.o%j "
                f"--error=../logs/IMERG_{DATE_F}.e%j "
                f"--export=DATE_F={DATE_F} "
                f"process_data.sh"
            )
        else:
            raise ValueError(f"Unknown cluster: {cluster}. Exiting.")

        os.system(command)
        logging.info("Processing data")

    else:
        logging.info(
            "No new data. Will not submit compute job. Retrying in 15 minutes"
        )


if __name__ == "__main__":
    main()
