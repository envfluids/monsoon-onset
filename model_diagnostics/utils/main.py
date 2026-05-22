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
    parser.add_argument("--deterministic_model", type=str, required=True)
    parser.add_argument("--ensemble_model", type=str, required=True)
    parser.add_argument("--deterministic_input", type=str, required=False)
    parser.add_argument("--ensemble_input", type=str, required=False)
    parser.add_argument("--output_dir", type=str, required=False)
    args = parser.parse_args()
    date = args.date
    region = args.region
    output_dir = Path(args.output_dir) if args.output_dir else None
    deterministic_input = Path(args.deterministic_input) if args.deterministic_input else None
    ensemble_input = Path(args.ensemble_input) if args.ensemble_input else None

    base = Path(__file__).resolve().parent.parent.parent
    if region == "india":
        logging.info("Plotting diagnostics for India.")
        plot_circulation_india(
            base,
            date,
            args.deterministic_model,
            args.ensemble_model,
            output_dir=output_dir,
        )
        plot_precip_india(
            base,
            date,
            deterministic_model=args.deterministic_model,
            ensemble_model=args.ensemble_model,
            deterministic_input=deterministic_input,
            ensemble_input=ensemble_input,
            output_dir=output_dir,
        )
    elif region == "ethiopia":
        logging.info("Plotting diagnostics for Ethiopia.")
        plot_circulation_ethiopia(
            base,
            date,
            args.deterministic_model,
            args.ensemble_model,
            output_dir=output_dir,
        )
        plot_precip_ethiopia(
            base,
            date,
            deterministic_model=args.deterministic_model,
            ensemble_model=args.ensemble_model,
            deterministic_input=deterministic_input,
            ensemble_input=ensemble_input,
            output_dir=output_dir,
        )
    else:
        logging.warning(f"Region '{region}' not recognized. No diagnostics will be run.")

    logging.info("Diagnostics plotting complete.")

    

if __name__ == "__main__":
    main()
