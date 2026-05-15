from download_forecast import DEFAULT_LOOKBACK_DAYS, get_data
import logging
from pathlib import Path
import subprocess
import argparse

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

REPO_ROOT = Path(__file__).parent.parent.parent
logging.info(f"Repository root determined to be: {REPO_ROOT}")

def main():
    parser = argparse.ArgumentParser(
        description="Download one NCMRWF precipitation_amount NetCDF forecast file.",
    )
    parser.add_argument(
        "--date",
        help="Forecast cycle in YYYYMMDDT00 format, for example 20260512T00.",
    )
    parser.add_argument(
        "--check-blend",
        help="Check if blending is needed for the given forecast date.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="Number of recent 00z forecast dates to check when --date is omitted.",
    )

    args = parser.parse_args()
    date_f = get_data(date=args.date, max_days_back=args.lookback_days)

    if date_f is None and not args.check_blend:
        logging.info("No new forecast downloaded, skipping pipeline execution.")
        return
    else:
        logging.info(f"New forecast downloaded with date string: {date_f}. Proceeding to run blending pipeline.")
        if args.date:
            date_f = args.date
        cmd = [
            "python",
            str(REPO_ROOT / "blend" / "utils" / "main.py"),
            "--date",
            date_f,
            "--region",
            "india",
            "--model",
            "NCUM",
        ]
        subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()
