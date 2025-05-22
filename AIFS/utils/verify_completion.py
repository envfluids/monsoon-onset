from pathlib import Path
import logging
import argparse
import sys

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def main():
    parser = argparse.ArgumentParser(
        description="Process weather data for a given year"
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Date for the inference in YYYYMMDDTHH format",
    )
    args = parser.parse_args()
    date = args.date
    base = Path(__file__).resolve().parent.parent.parent

    AIFS_output_path = base / "AIFS" / "output"
    AIFS_tp_path = AIFS_output_path / "tp" / f"tp_{date}.nc"
    AIFS_sji_path = AIFS_output_path / "sji" / f"sji_{date}.nc"
    AIFS_tcw_path = AIFS_output_path / "tcw" / f"tcw_{date}.nc"

    NGCM_output_path = base / "NeuralGCM" / "output"
    NGCM_tp_path = NGCM_output_path / "tp" / f"tp_{date}.nc"
    NGCM_sji_path = NGCM_output_path / "sji" / f"sji_{date}.nc"
    NGCM_tcw_path = NGCM_output_path / "tcw" / f"tcw_{date}.nc"

    def check_file_exists(file_path):
        if file_path.exists():
            logging.info(f"File exists: {file_path}")
            return True
        else:
            logging.warning(f"File does not exist: {file_path}")
            return False

    # check that all files exist

    files_to_check = [
        AIFS_tp_path,
        AIFS_sji_path,
        AIFS_tcw_path,
        NGCM_tp_path,
        NGCM_sji_path,
        NGCM_tcw_path,
    ]
    all_exist = True
    for file in files_to_check:
        if not check_file_exists(file):
            all_exist = False
    if all_exist:
        logging.info("All files exist, initiating model blend process")
        sys.exit(0)
    else:
        logging.warning("Not all files exist, exiting")
        sys.exit(1)


if __name__ == "__main__":
    main()
