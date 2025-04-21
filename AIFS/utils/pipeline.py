from download_ic import get_data
import os
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

def main():
    DATE_F = get_data()
    # DATE_F = "20250410T18"
    if DATE_F:
        logging.info(f"IC download script was successful, new data available")
        logging.info(f"Initializing compute job for date: {DATE_F}")
        print(f"DATE: {DATE_F}")
        command = (
            f"sbatch "
            f"--job-name=AIFS_fc_{DATE_F} "
            f"--output=../logs/AIFS_fc_{DATE_F}.o%j "
            f"--error=../logs/AIFS_fc_{DATE_F}.e%j "
            f"--export=DATE_F={DATE_F} "
            f"run_model.sh"
        )

        os.system(command)
        print("Running model")

    else:
        logging.info("Will not run model, no new data to download. Retrying in 15 minutes")

if __name__ == "__main__":
    main()