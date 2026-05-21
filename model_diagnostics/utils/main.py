from pathlib import Path
from argparse import ArgumentParser

from india.circulation_main import plot_circulation as plot_circulation_india
from india.plot_precip import plot_precip as plot_precip_india

from ethiopia.circulation_main import plot_circulation as plot_circulation_ethiopia
from ethiopia.plot_precip import plot_precip as plot_precip_ethiopia

import logging

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

def main():
    parser = ArgumentParser(description="Run model diagnostics utilities.")
    parser.add_argument("--date", type=str, required=False, help="Initialization date in YYYYMMDDTHH format (e.g., 20250508T06).")
    parser.add_argument("--region", type=str, required=False, help="Region for which to run diagnostics.")
    parser.add_argument("--model", type=str, required=False, help="Model for which to run diagnostics, only used for Ethiopia", choices=["AIFS", "AIFS_ENS"])
    args = parser.parse_args()
    date = args.date
    region = args.region
    model = args.model

    base = Path(__file__).resolve().parent.parent.parent
    if region == "india":
        plot_circulation_india(base, date)
        plot_precip_india(base, date, model)
    elif region == "ethiopia":
        plot_circulation_ethiopia(base, date, model)
        plot_precip_ethiopia(base, date, model)
    else:
        logging.warning(f"Region '{region}' not recognized. No diagnostics will be run.")

    

if __name__ == "__main__":
    main()