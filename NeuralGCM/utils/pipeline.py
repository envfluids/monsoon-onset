from download_ncep import get_data
import os
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

def main():
    DATE_F = get_data()

    if DATE_F:
        logging.info(f"IC download script was successful, new data available")
        logging.info(f"Initializing compute job for date: {DATE_F}")
        command = (
            f"sbatch "
            f"--job-name=NGCM_fc_{DATE_F} "
            f"--output=../logs/NGCM_fc_{DATE_F}.o%j "
            f"--error=../logs/NGCM_fc_{DATE_F}.e%j "
            f"--export=DATE_F={DATE_F} "
            f"run_model.sh"
        )

        os.system(command)
        print("Running model")

    else:
        logging.info("Will not run model, no new data to download. Retrying in 30 minutes")

if __name__ == "__main__":
    main()