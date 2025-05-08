from download_imerg import get_data
import os
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def main():
    DATE_F = get_data()
    # DATE_F = "20250410T18"
    print(DATE_F)
    if DATE_F:
        logging.info("IMERG download script was successful, new data available")
        logging.info(f"Initializing compute job for date: {DATE_F}")
        command = (
            f"sbatch "
            f"--job-name=IMERG_{DATE_F} "
            f"--output=../logs/IMERG_{DATE_F}.o%j "
            f"--error=../logs/IMERG_{DATE_F}.e%j "
            f"--export=DATE_F={DATE_F} "
            f"process_data.sh"
        )

        os.system(command)
        logging.info("Processing data")

    else:
        logging.info(
            "No new data. Will not submit compute job. Retrying in 15 minutes"
        )


if __name__ == "__main__":
    main()
